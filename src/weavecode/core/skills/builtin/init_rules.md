---
name: init_rules
description: 为当前项目建立规则文件，沉淀仓库约定与常用命令
allowed_tools:
  - read_file
  - list_dir
  - write_file
  - bash
---
你要为当前项目建立一份规则文件，让后续的 Agent 一开始就了解这个仓库的约定。

## 一、先调查

优先读信息密度最高的来源：

- `README*`、根目录清单（`pyproject.toml` / `package.json` / `Cargo.toml`）、锁文件
- 构建 / 测试 / lint / 格式化 / 类型检查的配置
- CI 工作流、pre-commit 与任务运行器配置
- 已有的规则文件（`.weave/context.md`、`~/.weave/context.md`）

读完配置和文档后如果架构还不清楚，再有选择地读少量有代表性的源码，找出真正的入口和模块边界。不要随机翻叶子文件。
不要翻 `.git/`、`node_modules/`、虚拟环境与构建产物。

## 二、再写文件

把确认下来的约定写进项目的 `.weave/context.md`（目录不存在就先创建），内容包括：

1. 跑测试、lint、类型检查的准确命令，以及它们的先后顺序
2. 从文件名看不出来、但很重要的架构说明
3. 这个仓库特有的约定、禁忌与踩过的坑

只写这一个文件，不要改动项目里的其他任何文件。

## 三、汇报

写完后向用户说明：写到了哪个文件、加了哪些条目。
