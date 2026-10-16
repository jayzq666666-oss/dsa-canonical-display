# DSA Canonical Display

将原始未减影的 `LOG + SUB` 多帧 DSA DICOM 转换为可供显示和后续模型读取的 12-bit `DISP` DICOM。算法来自现有 `canonical_display_v2`，已拆出为独立 Python 包，不依赖原 Web 平台或模型服务。

核心流程：按 DICOM 指定掩膜帧求均值 → 逐帧减影 → 轻量空间保边降噪 → 非对称 tanh 软映射 → RLE Lossless DICOM。已有 `DISP` 数据直接沿用。

[English README](docs/README_EN.md) · [算法与适用范围](docs/ALGORITHM.md) · [来源和提取范围](docs/PROVENANCE.md) · [发布说明](docs/PUBLISHING.md)

## 安装

需要 Python 3.11 或更新版本。以下命令在仓库根目录运行：

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

基础依赖为 NumPy 和 pydicom。RLE 输出使用 pydicom 内置编码器；源文件若使用 JPEG/JPEG-LS/JPEG 2000 压缩，需要另行安装与源传输语法对应的像素解码依赖。无法解码的文件会报错。

## 用合成数据运行

```bash
python examples/make_synthetic.py --output demo
dsa-display inspect demo/source/synthetic-raw.dcm
dsa-display convert demo/source/synthetic-raw.dcm demo/single.display-v2.dcm
dsa-display batch --raw-index demo/raw-index.csv --effective-index demo/effective.csv --source-root demo/source --derived-root demo/derived --audit-csv demo/audit.csv --summary-json demo/summary.json --workers 2
python -m pytest
```

也可用 `python -m dsa_canonical_display` 替代 `dsa-display`。`dsa-display profile` 输出固定参数及其 SHA-256。示例只生成合成像素和虚构标签。

## 处理自己的数据

先检查代表性文件：

```bash
dsa-display inspect /path/to/raw.dcm
```

- `DERIVE`：符合当前支持的 `LOG/SUB/AVG_SUB` 条件，可减影。
- `PASSTHROUGH`：已有 `DISP`，或未请求 SUB；批处理保留原路径，不额外复制。
- `UNSUPPORTED`：请求了 SUB，但标签或掩膜操作不受支持；批处理不发布新有效索引。

批量输入为 UTF-8 CSV，必须包含 `source_path` 列。其他列原样保留；一行可以用分号连接多个源文件。相对路径以输入 CSV 所在目录为基准；路径本身不能包含分号。

```csv
study_uid,series_uid,source_path
example-study,example-series,source/raw-a.dcm;source/raw-b.dcm
```

`--source-root` 覆盖所有源文件；`--derived-root` 必须与其分离，不能互相嵌套。输出保留相对目录，将文件名改为 `<stem>.display-v2.dcm`；若不同源文件会映射到同一输出名称，预检拒绝执行。有效索引、审计和汇总必须使用不同路径并放在源目录之外。

```bash
dsa-display batch --raw-index /data/raw-index.csv --effective-index /runs/v2/effective.csv --source-root /data/source --derived-root /runs/v2/derived --audit-csv /runs/v2/audit.csv --summary-json /runs/v2/summary.json --workers 4 --reserve-free-gib 10
```

Windows 可使用带引号的盘符路径。`--workers` 默认 1，大文件可按 CPU、磁盘和文件句柄限制逐步增加。容量预检使用原项目的经验压缩比，不能替代实际磁盘监控。

## 输出与续跑

| 产物 | 内容 |
| --- | --- |
| `*.display-v2.dcm` | 16-bit 容器，12-bit 有效灰阶，MONOCHROME2，DISP，RLE Lossless |
| `*.dcm.presentation.json` | 参数哈希、源/产物哈希、像素语义哈希、掩膜帧及 UID |
| `audit.csv` | 每个唯一源文件的 DERIVED / PASSTHROUGH / UNSUPPORTED / FAILED 状态 |
| `summary.json` | PROCESSING / READY / FAILED、参数、计数和错误详情 |
| `effective.csv` | 仅当本次所有文件成功时原子替换的有效索引 |

相同命令可续跑：只有源文件、参数和产物哈希均一致时才复用已有结果。失败时已完成的产物保留；若有效索引原先已存在，失败不会覆盖它，消费者必须检查**本次**汇总为 `READY`，避免读取旧索引。运行前的输入/路径预检失败只返回错误，不生成新的报告。

源像素和源文件保持不变，输出生成新的 Series/SOP UID，并保留 Study UID、帧数、帧时序和来源引用。不要同时启动多个任务写入同一输出目录。极长 cine 会产生较多临时帧文件和打开的文件句柄。

## Python API

```python
from dsa_canonical_display import derive_presentation_dicom

result = derive_presentation_dicom("raw.dcm", "derived/display.dcm")
print(result.status, result.artifact_sha256)
```

`derive_presentation_dicom` 仅接受可减影文件；混合原始/显示域数据请使用 `preprocess_series_index` 或 `batch`。API 在输出不是有效断点时会重新生成该输出，调用方应指定专用派生路径。CLI 对缺少断点 sidecar 的已有输出会拒绝覆盖。

## 参数与验证边界

当前固定参数：背景中心 3013，负向增益 5.5，正向增益 4.0，输出 0–4095。保留原 v2 的减影、降噪和软映射，不按每个序列的极值拉满灰阶，也不做时间平滑或运动配准。

测试覆盖合成 DICOM 减影、RLE 解码、时序保留、噪声/边缘、失败索引保护、断点校验、多进程、路径冲突和 CLI。GitHub Actions 配置了 Windows/Linux、Python 3.11–3.13；云端通过状态应以实际运行结果为准。

这是研究预处理代码。固定增益来自原项目数据域；更换设备、协议或输入模型后，需要重新核对灰阶、伪影和模型表现。`PASSTHROUGH` 只表示不执行减影，不表示已验证模型适配。

转换保留患者标签、私有标签、来源 UID 和可能的烧录文字；它**不执行脱敏**。派生 DICOM、CSV、sidecar 和日志应存放在受控数据目录。仓库只含代码和合成示例，`.gitignore` 排除常见运行数据，提交前仍应检查待提交内容。

## 许可证

[MIT](LICENSE)。
