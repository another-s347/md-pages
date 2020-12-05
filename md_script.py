from os import listdir
from os.path import isfile, join
from shutil import copyfile

def checked_copy(src, dst):
    copyfile(src, dst)

def list_md(path):
    return [f for f in listdir(path) if isfile(join(path, f))]

def main():
    zh_mds = list_md("./md/zh")
    for md in zh_mds:
        if isfile(join("./md/en", md)):
            checked_copy(join("./md/zh", md), join("./hexo/source/_posts",md))
            checked_copy(join("./md/en", md), join("./hexo-en/source/_posts",md))

if __name__ == "__main__":
    main()