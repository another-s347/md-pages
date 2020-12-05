## 使用PyO3跨越编程语言的异步

### 开始之前

这篇文章需要一些对异步的基本知识和理解。
感谢任何对本文技术和非技术相关问题的指正，例如语言表达之类的。

### Python中的初级异步

在Python中，一个基本的异步函数长这样，看起来和其它语言里面的异步语法很像（例如JavaScript或C#）。

```py
async def read(socket):
    data = await socket.read(1000)
    ...
```

语法糖之下，`read`本身也只是一个普通的函数，在Python中也是`callable`的。但是，在普通的上下文中调用这个函数`read`，会返回一个[`coroutine object`（协程对象）](https://www.python.org/dev/peps/pep-0492/#coroutine-objects).要记住，这里（我们定义的）`read`的函数体其实并没有执行。

```py
>>> read("whatever")
<coroutine object read at 0x.......>
```

一个协程对象就像是Rust中`Future`类型的一个实例，并且都是无栈（Stackless）的。这个对象里面包含了执行流程（即我们定义的函数体）、变量、状态等等。在Python中，有很多顶层的API可以创建协程对象。例如`@asyncio.coroutine`注解，基于生成器（generator）的协程，等等，但是在本文中并不会讨论这些API。我们需要想个办法，手工实现异步（协程对象）。具体一点地说，我们想写一个，能够`await`的类。

### 初步实现一个能够`await`的类

根据PEP492中的相关内容（自行翻译版）:
> await关键词只接受一个能await的对象，具体得是以下中的某一种：
>
> （...不重要...）
> 一个带有`__await__`方法的对象，这个方法返回一个迭代器对象（iterator）

这会是一个好的开始，但是说的不太清楚。实际上，`__await__`方法既能直接返回自己，也能返回几个新的对象。

这个返回的迭代器对象才是我们真正关注的对象（虽然我们也可以把所有方法塞在一个类里面）。这里根据asyncio简单的描述一下这个对象的相关内容。asyncio的执行器（executor）会调用对象的`__next__`方法，从而试图在异步任务上取得进展。这个方法类似于`rs:Future`中的`poll`方法。如果迭代器对象成功取得了进展（例如已得到远端返回的数据），则它的`__next__`方法需要抛出`StopIteration(result)`来告诉执行器执行结束。

```py
import asyncio

class MyAwait:
    def __init__(self, n) -> None:
        self.n = n
        pass

    def __next__(self):
        raise StopIteration(self.n)

    def  __await__(self):
        self.n += 1
        print("await on MyAwait")
        return MyIter(self.n)

class MyIter:
    def __init__(self, n) -> None:
        self.n = n

    def __next__(self):
        raise StopIteration(self.n)

async def main():
    a = MyAwait(1)
    print(await a) # 会打印出 "await on MyAwait" 和 "2"
    print(await a) # 会打印出 "await on MyAwait" 和 "3"

asyncio.run(main())
```
现在我们有了第一个属于自己的可以`await`的对象。但是这个对象没什么用，因为它不能表达“未决定”状态（Pending），也不能与执行器交互。（执行器在asyncio的源码里名为`loop`）。为了完善我们的实现，需要在asyncio的源码里找一些线索。

### Asyncio
如果MyIter的`__next__`方法返回整形1，我们会得到这样一个错误：
```
Task got bad yield: 1
```
这个错误源于asyncio的源码，位于 `/usr/lib/python3.8/asyncio/tasks.py:__step at line 340`.

事实上，Python中并没有标准或者PEP规定执行器需要怎么设计。但是既然asyncio是Python异步生态中的官方库，我们将以asyncio作为事实标准进行参考。当然，你也可以设计自己的协程对象和执行器，不与asyncio兼容也可以。

有关asyncio的第一个关键点在于，asyncio自己定义了一套表达异步任务的类型。为了避免混淆和误解，在这里对比一下相关的类型：

1. Python的[`Future` (PEP-3184)](https://www.python.org/dev/peps/pep-3148/): 这个类型来自早期的Python，用来表达未来的结果，当时还没有成熟的异步框架。
2. asyncio的`Future`: 与python的`Future`兼容，只有细微的区别和一些新方法，位于`asyncio/futures.py`。我们的类型要么继承这个`Future`，要么与其保持兼容。
3. asyncio's `Task`: 表示一个异步任务的实例，用来实际控制这个异步任务的执行。位于`asyncio/tasks.py`。

`asyncio/tasks.py`中的`__step`方法是我们需要关注的地方。阅读源码，去掉一些检查后发现，重要的代码在298行-320行：
```py
# `result`即为我们创建的迭代器对象MyIter
blocking = getattr(result, '_asyncio_future_blocking', None)
if blocking is not None:
    # Yielded Future must come from Future.__iter__().
    if futures._get_loop(result) is not self._loop:
        new_exc = RuntimeError(
            f'Task {self!r} got Future '
            f'{result!r} attached to a different loop')
        self._loop.call_soon(
            self.__step, new_exc, context=self._context)
    elif blocking:
        if result is self:
            new_exc = RuntimeError(
                f'Task cannot await on itself: {self!r}')
            self._loop.call_soon(
                self.__step, new_exc, context=self._context)
        else:
            result._asyncio_future_blocking = False
            result.add_done_callback(
                self.__wakeup, context=self._context)
            self._fut_waiter = result
            if self._must_cancel:
                if self._fut_waiter.cancel():
                    self._must_cancel = False
```

可以看出我们需要:

1. 一个`_asyncio_future_blocking`属性 (类型为 bool)来告诉执行器：这个任务当前是否被执行器阻挡（block）了（被阻挡的意思是 执行器当前不打算尝试这个任务）。其余细节能在`asyncio/futures.py`中找到。
2. 一个`_loop`属性，返回负责此任务的执行器实例.
3. 一个`add_done_callback`方法，用来存储由执行器提供的回调，这个回调应当在异步任务完成时调用。这个回调的功能类似于Rust中的`waker`结构。要注意，这个回调不应该被多次调用，因此需要注意做一些检查和清理工作。如果`add_done_callback`在异步任务完成后才被调用，回调就应当被立即执行，否则我们的任务就不会在执行器中唤醒。 
4. 一个`result`方法返回异步任务的结果，或者异步执行途中产生的异常（exception）。asyncio调用这个方法来进行异常检查，而不是真正的返回结果。真正的返回结果还是得靠`__next__`中的`StopIteration`。

### 尝试一下
一个最小的，与asyncio兼容的自制协程对象大概长这样:
```py
class MyAwait:
    def __init__(self, n) -> None:
        self.n = n
        pass

    def __next__(self):
        raise StopIteration(self.n)

    def  __await__(self):
        self.n += 1
        return MyIter(self.n)

class MyIter:
    def __init__(self, n) -> None:
        self.state = 0
        self.n = n
        self._asyncio_future_blocking = True
        self._loop = asyncio.get_event_loop()
        self.callbacks = []

    def add_done_callback(self, m, context):
        if self.state == 1: 
            self.callbacks.append((m, context))
        else:
            self._loop.call_soon(m, self, context=context)

    def __next__(self):
        if self.n != 5:
            self.n += 1

            for m, c in self.callbacks:
                self._loop.call_soon(m, self, context=c)
            self.callbacks = []

            self._asyncio_future_blocking = True
            return self

        self.state = 1
        raise StopIteration(self.n+1)

    def result(self):
        return "whatever"
```

### 引用与参考

* [PEP 492 -- Coroutines with async and await syntax](https://www.python.org/dev/peps/pep-0492/)

#### License

本文采用[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh)协议进行授权。