---
name: git-commit-message
description: 按 Conventional Commits 规范生成 git 提交信息（type/scope/subject/body），支持中文与英文。Use when 用户要写 commit message、提交说明、帮我提交代码、生成 git 提交信息、整理这次改动怎么写时。不要用于写 PR 描述或 release notes。
version: 0.1.0
license: MIT
allowed-tools: "Bash(git:*)"
---

# Git 提交信息生成

## 规范（Conventional Commits）

```
<type>(<scope>): <subject>

<body>

<footer>
```

- **type**（必填）：`feat` 新功能 / `fix` 修复缺陷 / `docs` 文档 / `style` 格式（不影响逻辑）/
  `refactor` 重构 / `perf` 性能优化 / `test` 测试 / `build` 构建或依赖 / `ci` CI 配置 /
  `chore` 其他杂项 / `revert` 回滚
- **scope**（可选）：影响的模块，如 `api`、`frontend`、`sandbox`。
- **subject**（必填）：一句话说明**做了什么**。
  - 祈使句、现在时（"添加" 而非 "添加了" / "add" 而非 "added"）
  - 不加句号结尾
  - 控制在 50 字符内（中文约 25 字）
- **body**（可选）：说明**为什么**这么改，而非重复做了什么。每行 ≤72 字符，段落间空行。
- **footer**（可选）：`BREAKING CHANGE:` 破坏性变更；`Closes #123` 关联 issue。

## 流程

1. 先拿到**实际改动**：优先用 `git diff --staged`（已暂存）或 `git diff`；
   拿不到改动就向用户要，绝不凭空编造。
2. 判断改动属于哪个 type —— 一个提交只做一件事。
   若混了多类改动（既修 bug 又加功能），**建议拆成多个提交**并分别给出信息。
3. 写 subject：读者扫一眼提交历史就能明白的程度。
4. 补 body：写清动机、取舍、以及无法从代码看出的上下文。
5. 输出时给出**可直接复制的命令**：

```bash
git commit -m "type(scope): subject" -m "body 第一行
body 第二行"
```

## 硬性要求

- **绝不编造改动内容**。没有 diff 就说明需要哪些信息。
- 不要用含糊的 subject：`更新代码`、`修改 bug`、`优化`、`fix stuff`。
- 不要在一次提交信息里塞进多篇无关的改动说明。
- 用户没要求时不要自动执行 `git commit`，只给命令。
