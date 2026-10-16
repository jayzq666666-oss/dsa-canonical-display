# 发布与维护

该仓库仅包含独立预处理算法；真实数据、运行目录和模型服务均不在发布范围内。MIT 许可证由项目所有者选择。

本地检查：

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m build
git status --short
git diff --cached --stat
```

推送前检查暂存清单，确认只含源码、合成测试、示例生成器、文档和 CI。ZIP 交付包不含 Git 历史和运行数据。

数值 profile 沿用 `canonical_display_v2`。后续若修改减影逻辑、滤波参数、增益或元数据语义，应升级 profile ID 和输出代际，重新检查 profile hash、UID、断点与模型兼容性。Python 包版本与数值 profile 版本独立。

本地验证只证明当前测试环境和合成数据覆盖的行为。历史真实病例统计不作为本仓库的已复现性能声明。GitHub Actions 推送后会在 Windows/Linux、Python 3.11–3.13 上执行合成测试和构建。
