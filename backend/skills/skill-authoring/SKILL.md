---
name: skill-authoring
description: 编写或改进 SKILL.md（Agent Skills 开放标准）的规范、结构与自查清单，含 frontmatter 规则、三级渐进式披露与 description 写法。Use when 用户要创建/编写/修改一个 Skill、写 SKILL.md、问 Skill 该怎么组织，或想把一个重复流程固化成 Skill 时。
version: 0.1.0
license: MIT
---

# 编写 Skill

Skill 是**一个文件夹**，不是单个文件：

```
your-skill-name/
├── SKILL.md          # 必需：YAML frontmatter + Markdown 正文
├── scripts/          # 可选：可执行代码（被执行，不进上下文）
├── references/       # 可选：按需加载的参考文档
└── assets/           # 可选：模板、字体、图标等产出物
```

## 三级渐进式披露（核心机制）

| 层级 | 内容 | 何时加载 |
|---|---|---|
| L1 | frontmatter 的 name + description | 常驻 system prompt，约 100 token/skill |
| L2 | SKILL.md 正文 | 模型判断命中后，用 `use_skill` 加载 |
| L3 | scripts/ references/ assets/ | 正文明确要求时才读取或执行 |

**因此装几十个 Skill 也不会撑爆上下文**，代价是：description 写得不好，Skill 就永远不会被触发。

## frontmatter 规则

| 字段 | 必填 | 规则 |
|---|---|---|
| `name` | 是 | kebab-case，≤64 字符，与文件夹名一致，不得含 `anthropic` / `claude` |
| `description` | 是 | ≤1024 字符，必须同时说清**做什么**和**何时用**，第三人称，含触发词 |
| `version` / `license` / `allowed-tools` | 否 | `allowed-tools` 用于最小权限约束 |

frontmatter 会注入 system prompt，**禁止使用 XML 尖括号**。

## description 写法（最高杠杆字段）

结构：`[做什么] + [何时用] + [触发词/文件类型]`，必要时加**反向触发**。

- 好：`分析 Excel 表格、生成透视表与图表。Use when 处理 .xlsx / 表格数据 / 需要透视分析时。`
- 差：`帮你处理文档`

写完后自检一句："用户会怎么说出这个需求？"——把这些说法写进去。

## 正文写法

- 控制在 500 行以内；接近上限就拆到 `references/`，正文只留索引与链接。
- 用**命令式**写步骤，而不是描述性散文。
- 明确执行意图："运行 `scripts/analyze.py`"（执行）vs "算法见 `scripts/analyze.py`"（当作参考读）。
- 参考文件超过 100 行时，在文件开头加目录。
- 只写模型不知道的东西：项目约定 > 通用常识。

## 自查清单

- [ ] name 是 kebab-case 且与文件夹名一致
- [ ] description 同时说明"做什么"与"何时用"，含真实触发词
- [ ] 正文 <500 行，长内容已拆到 references/
- [ ] 每个步骤都是可执行的命令式指令
- [ ] 引用资源时写清"执行"还是"阅读"
- [ ] 无 XML 尖括号，无与全局规则冲突的指令
- [ ] 已用真实任务试跑，确认能被正确触发

## 更多细节

- description 写法与反例：见 `references/description-guide.md`
