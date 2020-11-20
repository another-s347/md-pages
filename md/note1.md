## Async across language with pyo3

### Before get started



### Basic async in python

A basic async function in python looks like this, which is very similar to async in other language like JavaScript or C#.

```py
async def read(socket):
    data = await socket.read(1000)
    ...
```

`read` itself is just a normal function. But by calling this function `read` under normal context (without async), it produces a [`coroutine object`](https://www.python.org/dev/peps/pep-0492/#coroutine-objects). Noted, the body of `read` is not actually executed.

```py
>>> read("whatever")
<coroutine object read at 0x.......>
```

The coroutine object is like a instance of `future` in Rust and it is stackless. It contains the execution flow, variables, states...etc. There are many high-level ways to create a coroutine object, like `@asyncio.coroutine` annotation, generator based coroutine. But we won't coverd them here. We need a way to implement async by hand. Specifically, we want to write a class which can be `await`ed.

### Implement a basic awaitable object

According to PEP492:
> await only accepts an awaitable, which can be one of:
>
> blablabla
>
> An object with an `__await__` method returning an iterator.

This is a good start. The `__await__` method can return itself, or create multiple different objects. 

The returned iterator is the object we actually want. In a simplified context, the executor of asyncio (or any other library) would call `__next__` on the iterator to make progress on the task. It's like the `poll` in Rust's `Future`. If the object get its result (for example the data from remote peer), the `__next__` need to call `raise StopIteration(result)` to finish the execute.

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
    print(await a) # will print "await on MyAwait" and "2"
    print(await a) # will print "await on MyAwait" and "3"

asyncio.run(main())
```
Now we have our first custom awaitable object. But it is useless, because it do not have anything to express pending status or do anything with the executor (the loop in the source code of asyncio). We need to find some clue in the source code.

### Asyncio
If the `__next__` in MyIter return a int like 1, we will have a error like this:
```
Task got bad yield: 1
```
This error is raised from the code of asyncio, located at `/usr/lib/python3.8/asyncio/tasks.py:__step at line 340`.

In fact, there is no standard or PEP of how to design the executor. Since `asyncio` is the offical library in the ecosystem of async python, we will stick to it to find our answers. And of course, you can design your own object and executor that is not compatible with asyncio.

The first key point with asyncio is: asyncio has its own type to represent async task. To avoid misunderstanding, I will list related types here:

1. python's [`Future` (PEP-3184)](https://www.python.org/dev/peps/pep-3148/): a old type to express result from the future, or async as we call here.
2. asyncio's `Future`: almost compatible with python's `Future`, with some differences and new methods. Located at `asyncio/futures.py`. This is the type our object should be based on or be compatible with.
3. asyncio's `Task`: a instance of async task that controls the execution of our code. Located at `asyncio/tasks.py`.

The `__step` in `asyncio/tasks.py` is where we should foucs on. After digging around and ignoring dozens of type-check, the important lines are 298-320:
```py
# `result` is the iterator we created
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

We can see that we need:

1. a `_asyncio_future_blocking` attribute (type bool) to tell the executor whether it is blocked (by executor) currently. Details can be found at `asyncio/futures.py`.
2. a `_loop` attribute that gives the executor running this task.
3. a `add_done_callback` method to store the callback from executor, the callback should be called when our async task is done. The callback is like the `waker` in Rust. Noted that the callback should not be called multiple times. So some cleanup and checks are needed. If the `add_done_callback` is called after it is done, the callback should be called immediately or our task won't get waked from executor.
4. a `result` method which return the result of the future or exception if any. Asyncio calls it for exception checking, not actually return the result.

### Take a shot
A minimal, asyncio-compatible custom awaitable would be like this:
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

### Reference

* [PEP 492 -- Coroutines with async and await syntax](https://www.python.org/dev/peps/pep-0492/)