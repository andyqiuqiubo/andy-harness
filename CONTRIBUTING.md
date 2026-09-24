# 贡献指南

感谢你对 andy-harness 项目的兴趣！欢迎提交 Issue、PR 和建议。

---

## 开发环境搭建

```bash
# 克隆仓库
git clone https://github.com/your-org/andy-harness.git
cd andy-harness

# 安装依赖
make install

# 启动开发服务器
make backend  # 终端 1
make frontend # 终端 2
```

详细步骤请参考 [README.md](README.md)。

---

## 代码规范

### 后端（Python）

- **格式化**：`ruff format .`
- **检查**：`ruff check . && mypy harness`
- **行宽**：120 字符
- **Python 版本**：3.11+
- **类型注解**：必须添加类型注解（mypy strict 模式）

```bash
make lint     # 检查
make format   # 格式化
```

### 前端（TypeScript）

- **格式化**：`prettier --write src/`
- **检查**：`eslint .`
- **框架**：Vue 3 Composition API + `<script setup>`
- **状态管理**：Pinia
- **类型**：必须添加 TypeScript 类型

```bash
cd frontend
pnpm lint
pnpm format
```

### 提交前

```bash
make lint  # 确保检查通过
make test  # 确保测试通过
```

pre-commit hook 会自动执行检查。

---

## 提交 PR

1. Fork 仓库并创建分支：
   ```bash
   git checkout -b feature/my-feature
   ```

2. 编写代码，确保：
   - 代码通过 lint 和测试
   - 新功能有对应测试
   - 如有新 API，更新文档

3. 提交 commit（使用清晰的 commit message）：
   ```bash
   git commit -m "feat: 添加 XXX 功能"
   git commit -m "fix: 修复 XXX 问题"
   git commit -m "docs: 更新 XXX 文档"
   ```

4. Push 并创建 PR：
   ```bash
   git push origin feature/my-feature
   ```

5. 在 PR 描述中说明：
   - 变更内容
   - 变更原因
   - 测试方式

---

## 开发插件

请参考 [插件开发指南](docs/plugin-dev-guide.md)。

---

## 项目架构

请阅读 [架构设计文档](01-architecture.md) 了解项目分层和模块设计。

---

## 行为准则

- 保持友善和尊重
- 欢迎新手提问
- 代码审查时关注建设性反馈
- 尊重不同观点和背景
