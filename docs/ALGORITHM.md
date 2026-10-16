# canonical_display_v2 算法

## 输入判定

优先直接沿用 `PixelIntensityRelationship=DISP`。未请求 `RecommendedViewingMode=SUB` 的输入也沿用，不会推测未标记数据的语义。

仅处理 `LOG + SUB`、单条 `MaskSubtractionSequence`、`MaskOperation=AVG_SUB`、单通道 MONOCHROME1/2、1–8 个合法不重复掩膜帧。掩膜帧采用 DICOM 的 **1-based** 编号；不同掩膜帧求算术均值。若存在 ApplicableFrameRange、ContrastFrameAveraging、MaskSubPixelShift 或 MaskVisibilityPercentage，拒绝处理。

算法按存储像素做差，不再次取对数，不应用额外的 rescale、VOI 或 MONOCHROME1 反转。当前数据域假设血管对应负差值；改变输入编码时必须检查该假设。未实现自动掩膜选择、旋转补偿、运动配准及设备专用工作站处理。

## 减影

对掩膜编号集合 M：

```text
mask = mean(raw[m - 1] for m in M)
delta[t] = float32(raw[t]) - mask
```

掩膜累加使用 int64，均值与逐帧计算使用 float32。每一帧都有输出，包括掩膜帧；多掩膜情况下各掩膜帧与其均值的差不一定为零。

## 空间降噪

使用边缘复制填充和 3×3 二项式核：

```text
K = [[1, 2, 1], [2, 4, 2], [1, 2, 1]] / 16
smooth = K * delta
residual = delta - smooth
sigma = max(0.5, 1.4826 * MAD(residual in central 60% ROI))
u = clip((abs(residual) - 0.75*sigma) / (2.25*sigma), 0, 1)
weight = u*u*(3 - 2*u)
filtered = smooth + residual*weight
```

低幅高频残差向平滑结果收缩，强残差逐渐保留。此滤波只发生在单帧空间域；强边缘与噪声无法保证在所有数据域中完全区分。

## 软映射

令 d 为滤波后的差值，N=3013，P=4095−3013=1082：

```text
d <= 0: y = N - N*tanh((-d*5.5)/N)
d >  0: y = N + P*tanh((d*4.0)/P)
output = uint16(round(clip(y, 0, 4095)))
```

负差值映射为较暗血管，正差值平滑趋向高灰阶。不会根据观察到的极值伸展显示范围；极大差值仍可能因浮点精度和整数取整达到 0/4095。审计中的 low_delta/high_delta 是两侧 tanh 特征尺度（约 −547.82/+270.5），不是样本分位数，也不是硬截断阈值。

## DICOM 封装

输出使用 16-bit 无符号容器、BitsStored=12、HighBit=11、MONOCHROME2、DISP、RLE Lossless；窗位/窗宽为 2047/4095，RescaleSlope/Intercept 为 1/0。去掉减影指令和原 VOI LUT、padding、像素极值标签；保留其余原标签及帧时序。未全面重写所有设备专用 LUT/私有元数据，需在接收端核对显示。

新 Series/SOP UID 由 profile ID、源 UID 和类型确定；像素语义哈希还纳入 profile、形状和时序。源文件哈希、产物文件哈希和 profile hash 用于断点复用。此 scheme 沿用原算法：修改像素或参数时应保留合法源 UID 规则，并使用新 profile ID/新输出代际，避免 UID 与内容发生歧义。

临时 RLE 帧、临时 DICOM 和 sidecar 在输出目录中写入，成功后原子替换文件。文件和 sidecar 是分步提交；中断留下不完整断点时下次重新生成。批次不是全目录事务，只有有效索引在全部成功后发布。
