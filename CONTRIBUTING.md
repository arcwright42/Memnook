# 开发与包治理

## 包边界

```text
interfaces → application → storage / providers → domain → config
```

- `interfaces`：MCP 协议映射、CLI、应用装配与生命周期。
- `application`：提炼流程、任务执行、召回融合。通过 domain 的 Protocol 使用模型能力。
- `storage`：PostgreSQL 表映射、事务、队列、查询、写入。SQL 只放在这一层。
- `providers`：模型协议、HTTP 请求和返回校验。不访问数据库。
- `domain`：业务入参/返回模型、模型接口、纯排序和文本合并逻辑。只依赖标准库和 Pydantic。
- `config`：环境配置与配置身份计算，不导入其他业务包。

import-linter 在 CI 中检查层次关系，并禁止 storage 与 providers 相互依赖。禁止通过动态 import 绕过检查，禁止在 `__init__.py` 中创建连接或调用模型。测试可以跨层装配依赖，生产代码必须遵守边界。

## 依赖规则

1. 用 `uv add` / `uv remove` 修改依赖，同时提交 `pyproject.toml` 与 `uv.lock`。
2. 运行时依赖与 dev 依赖分开，直接 import 的第三方包必须直接声明；deptry 检查遗漏和未使用依赖。
3. 运行时声明兼容范围，锁文件固定实际版本和 artifact hash。开发与 CI 用 `uv sync --locked`，镜像用 `--frozen --no-dev`。
4. 单个包升级使用 `uv lock --upgrade-package 包名`，检查变更范围，运行受影响测试与包检查。新增基础设施应给出实际需求。
5. jieba 目前使用 `pkg_resources`，因此显式声明 setuptools 并限制在 80.x；这是兼容性依赖，替换 tokenizer 时一并移除。
6. 生产镜像不包含测试工具。密钥、数据库文件、下载工具和实验输出分别留在 `.env`、数据卷、`work/`、`outputs/`，不得进入分发包。

构建后端 Hatchling 单独固定版本；升级后必须重新验证 sdist、wheel 与安装后的迁移入口。CI 对 Python 3.12 和 3.13 执行同一套检查。

## 数据库与协议变更

- 已发布的 Alembic 迁移保持不变，新变更新增 revision。初始迁移不导入会变化的 ORM 模型。
- JSON Schema 与 MCP 返回体属于外部契约。修改后更新 `docs/api.md`，验证真实 MCP HTTP 调用。
- 数据库使用包含 tag 的复合外键约束图和来源关联，所有查询仍需明确过滤 tag。
- 网络调用和模型推理发生在事务外；任务租约检查、结果写入和 completed 状态在同一事务内完成。

## 提交前检查

执行 README 的 lint、边界、依赖、测试、构建命令。涉及存储或 Worker 时必须连接专用 PostgreSQL 测试库，不能把 skipped 当作集成测试通过。

构建后在独立环境安装 wheel，验证 `memory --help`、迁移资源可定位、`memory migrate` 可运行。wheel 必须只包含运行时代码、迁移资源和包元数据；sdist 的内容由显式白名单控制。

MCP 接口与 CLI 是当前对外入口；内部包尚不承诺独立 SDK 兼容性。发布前再确定正式项目名与许可证，本工程不会自动发布包。
