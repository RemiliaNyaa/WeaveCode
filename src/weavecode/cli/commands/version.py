import weavecode


# 打印当前 weavecode 包的版本号
def cmd_version() -> None:
    print(weavecode.__version__)
