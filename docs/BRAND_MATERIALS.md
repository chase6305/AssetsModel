# 品牌外观与 PBR 材质

2026-11-03 首轮更新对任务开始时十个机器人品牌的 **367 个外观 GLB、563 个材质分区**进行了调整。随后补齐了 ABB IRB1200-5/0.9 的 Link5：这是实际被 URDF 引用、却没有嵌入材质的外观零件。它从 URDF 的 `ABBWhite` 解析原色，并使用与同机型外壳一致的白色漆面配置。共有 **368 个外观 GLB** 更新；基线 389 个机器人外观 GLB 中剩余 **21 个**无嵌入材质的文件没有被基线 URDF 外观引用，保持原样。新增机器人使用各自导入记录中的上游材质，本轮不以旧模型的调色规则推断新型号外观。

本次优化保留机械结构、原材质分组、商标、控制按钮和状态灯的区分。烤漆、塑料、橡胶、金属采用不同的反射参数，解决原 CAD 大量 `roughness=1` 导致的平板感，以及 FR3 部分塑料表面过亮的问题。材质数值是用于可视化的近似，不是厂家认证色号或实测 BRDF。产品照片存在光照、曝光和显示差异，不能据此声称精确还原生产涂层。

| 品牌 | 本轮外观处理 | 依据与保留内容 |
| --- | --- | --- |
| ABB | 细调暖白烤漆，保留红色商标与深色关节 | [ABB IRB1200 产品资料](https://new.abb.com/products/robotics/nl/onze-robots/articulated-robots/irb-1200)展示的白色机身；不把旧型号重涂成新产品代际 |
| AE / 配天 Peitian | 原黄色分区改为暖金黄，保留黑色底座与标识 | [配天 AIR10-1210 官方产品页面](https://www.peitianrobot.com/desktop/271.html)与仓库型号对应；`AE` 是保留的资源目录名。第二轮以同型号产品图复核暖金黄色相，详见下文 |
| Agile | 保留浅色外壳与深色嵌面；法兰使用金属反射 | [Diana 7 产品页面](https://www.agile-robots.com/en/products/diana-7/)；实际网格并非整机深灰，保留浅色壳体、深色关节区域与商标的区别 |
| FANUC | 黄色机身与黑色盖板分开；底座和法兰按型号复核 | [FANUC M-10iD/12 官方彩页](https://www.fanucamerica.com/docs/default-source/fanuc-robot-datasheets-new/flyer-m-10id-12-en.pdf)中的黑底座保留；M-710、R-2000 与 M-20 的后续分区调整见下文。该规则不代表食品级等特殊涂装 |
| Franka | 白色外壳改为柔和的塑料表面，深灰部件与金属独立处理 | [Franka Research 3 官方页面](https://franka.de/franka-research-3)；保留按钮和指示区原始颜色，预览没有运行状态含义 |
| Han's Robot / Elfin | 白色外壳、深色关节与银色工具法兰分别处理 | [华沿 Elfin 官方产品页面](https://www.huayan-robotics.com/elfin)列出 E05/E10 及铝合金本体；保留 `Hans` 目录和原 CAD 分区。白色涂层采用非金属反射近似，不据此将本体基材称为塑料 |
| KUKA | 旧代及大型机保留橙色；新代 AGILUS 按标准白色处理 | [2017 年 KUKA 官方新 AGILUS 发布资料](https://www.kuka.com/en-se/company/press/news/2017/08/kr-agilus)明确白色成为标准、其它颜色可订制；按型号处理，不将橙色写入全部材质 |
| Yaskawa Motoman | 原近纯蓝改为偏青的工业蓝，保留深色末端 | [Yaskawa GP7 官方页面与产品照片](https://www.yaskawa.it/robot/movimentazione-assemblaggio/productdetail/product/gp7_692)；仅修改原蓝色材质组 |
| ROKAE | 保留原白/蓝分区，区分塑料与末端金属 | [ROKAE xMate SR 官方资料](https://static.rokae.com/Downloads/DataSheet/ROKAE_xMate%20SR_EN.pdf)；不将全部外壳改蓝 |
| Universal Robots | 粉蓝关节盖、深色密封圈、铝制臂管分别处理 | [Universal Robots 官方机器人页面](https://www.universal-robots.com/products/)；保留不同结构部件，不把整条手臂统一染色 |

## 调色与材质参数

下面的色值用于维护和复现，均是视觉近似。脚本先把 sRGB 色值转换为线性的 glTF `baseColorFactor`；不能把十六进制颜色归一化后直接当作线性数值。

| 分区 | sRGB 调色 | 金属度 | 粗糙度 |
| --- | --- | ---: | ---: |
| Peitian 暖金黄色漆面 | `#EFB719` | 0.00 | 0.36 |
| KUKA 橙色烤漆 | `#EF7421` | 0.00 | 0.36 |
| 新代 AGILUS 标准白漆 | `#E8EAE7` | 0.00 | 0.36 |
| FANUC 黄色烤漆 | `#F4CB16` | 0.00 | 0.36 |
| Motoman 蓝色烤漆 | `#006EB8` | 0.00 | 0.36 |
| UR 粉蓝塑料关节盖 | `#79AAC7` | 0.00 | 0.46 |
| 浅色烤漆 | `#E4E6E2` | 0.00 | 0.36 |
| 中性浅色塑料 | `#ECEEEF` | 0.00 | 0.46 |
| 深灰塑料 | `#343B40` | 0.00 | 0.46 |
| 铝制臂管 | `#BFC5CA` | 0.72 | 0.34 |
| 末端金属 | 保留源色或铝色近似 | 0.82 | 0.27 |
| 深色涂层 | 保留源色 | 0.00 | 0.46 |
| 橡胶/密封圈 | 保留源色 | 0.00 | 0.68 |
| 商标/控制区 | 保留源色 | 0.00 | 0.48 / 0.30 |

## 几何保真与复现

[逐文件记录](quality/materials.json)保存原始/更新后文件 SHA-256、嵌入 BIN 的 SHA-256、每个材质的前后值与表面角色。以下严格比较针对本次材质修改步骤及其独立备份；后续网格整理如改写 BIN，应另看该步骤的几何验证记录。367 个文件均通过了重新读取后的比较：

- BIN 字节完全一致，因此顶点、索引、法线及其他二进制资源没有变化。
- 删除 `materials` 后的 glTF JSON 完全一致，包括 mesh primitive 的材质索引、节点、坐标、访问器与场景。
- 更新后的文件通过仓库 GLB 格式检查；脚本没有写入碰撞文件、URDF 或工具模型。
- 再执行相同配置的 dry-run，变更数为 0；角色元数据避免根据调整后的颜色重新误分类。

预检只读：

```bash
python3 scripts/refine_materials.py --report /tmp/materials-proposed.json
```

需要写入时指定仓库外的备份目录；备份不覆盖不同内容：

```bash
python3 scripts/refine_materials.py --apply \
  --backup-dir /tmp/assetsmodel-materials-before-new-pass \
  --report /tmp/materials-applied.json
```

`--vendors KUKA UniversalRobots` 可限定品牌。脚本仅处理这些品牌模型的 `Visual/glb/*.glb`，不处理 `Collision`。默认跳过模型目录含 `provenance.json` 的新导入资源；因此新增 iiwa14 不会在日常重复执行时套用原有 KUKA 调色规则。只有显式传入 `--include-imported` 才会把已选品牌的新导入模型纳入处理。每个材质 `extras.assetsmodel_material` 记录 `profile`、`role` 和使用的调色名称，便于复查与后续有针对性的迭代。含 `baseColorTexture` 的材质保持原样，避免染色贴标。

预览必须读取嵌入 GLB 的线性基色、金属度和粗糙度；如果查看器统一覆盖材质或只采用 URDF 单色，无法观察到此更新完整的分区效果。画面亮度也会随环境光与色彩管理变化，请使用相同的预览设置比较前后。

## 缺失材质的单独修复

[嵌入记录](quality/embedded-materials.json)列出 ABB Link5 的 URDF 来源原色、最终嵌入材质和变更位置。此修复与上述 367 个文件的材质数组修改不同：它还给原本没有 `material` 的三角形 primitive 增加了材质索引；BIN、坐标、索引、法线及其它 JSON 字段仍完全不变。已经有材质的 primitive 不会被覆盖。

```bash
python3 scripts/embed_missing_materials.py --report /tmp/missing-materials.json
```

实际写入添加 `--apply --backup-dir /tmp/material-embed-before`。工具只扫描 URDF 实际引用的外观网格，不扫描可选 STL 转换备选；同一个 GLB 被多个变体引用时，先核对所有 URDF 色值。若色值冲突或无法解析明确色值，在写文件前拒绝操作。重复执行后没有待修复项。

## 第二轮配天暖金黄

对照 [AIR10-1210 官方产品页](https://www.peitianrobot.com/desktop/271.html) 的同型号产品图，原 CAD 黄色偏柠檬绿，产品呈更暖的金黄色。用相同实际 mesh、相机和灯光试渲染 `#E8AF18` 与 `#EFB719` 后，采用较亮的 `#EFB719` 视觉近似。只更新四款 AE 模型中已有黄色漆面组，共 20 个 GLB / 20 个材质；黑色底座、标识和所有二进制几何保持不变。

[第二轮逐文件记录](quality/materials-ae-pass2.json)保存此次前后值、文件与 BIN SHA。第一轮记录保留第一轮实际状态；这 20 个文件的当前颜色以第二轮记录为准。

## FANUC 按型号细化底座和法兰

根据同型号官方资料，M-710iC/70 与 R-2000iC/165F 的源 CAD 底座 `0.4` 线性灰在统一灯光下过亮，已分别采用 `#303437` 与 `#363A3D` 炭灰近似，保留独立的黑色转环。M-20iA 底座采用 `#424444`，区分底座与黑环；M-10iD/12 的原近黑底座保留。三款前端法兰采用 `#303437` 深色金属近似，原金属度和粗糙度不变。

依据包括 [M-710iC/70 官方彩页](https://www.fanucamerica.com/docs/default-source/robotics-files/fanuc-robot-datasheets-new/datasheet-m-710ic-70.pdf)、[R-2000iC/165F 官方彩页](https://www.fanucamerica.com/docs/default-source/fanuc-robot-datasheets-new/datasheet-r-2000ic-165f.pdf)及 [M-20iA/35M 官方产品图](https://www.fanucamerica.com/products/robot/m-20ia-35m)。M-20 文件名未注明载荷版本，随附资料标为 35M；20M 与 35M 官方图的这些颜色分区一致，但精确机械版本仍保留未核实说明。深色金属参数只用于可视化，不将照片推断称为实测涂层。

[型号复核](quality/model-material-review.json)记录来源、照片哈希与临时渲染；[实际写入记录](quality/materials-fanuc-components.json)记录六份文件的当前采用步骤。BIN 与非材质 JSON 完全相同。脚本使用型号路径、文件名与原材质名联合匹配，避免把其它型号、转环或电机盖一并染色。现有黄色保持 `#F4CB16`，没有采用收益不明确的黄色候选。

## 华沿法兰与新代 KUKA AGILUS

E05 与 E10 的 `Link6` 是独立的工具法兰，原文件却沿用白色外壳材质。对照[华沿 Elfin 产品资料](https://www.huayan-robotics.net/elfin-collaborative-robot)与官方末端近照后，已将这两个命名材质设为银色金属近似（`#BFC5CA`、金属度 0.72、粗糙度 0.34）。白色腕壳在其它 link 中，保持原色。侧面小 I/O 细节与法兰共用原材质，仍受源 CAD 分区限制。详见[两份法兰写入记录](quality/materials-hans-flanges.json)。

KR10_R900_2、KR10_R1100_2 与 KR3_R540 的外形对应官方新代 AGILUS 图片，默认外壳已按官方标准白处理，共 15 个已有漆面组。`sixx` 旧代及其它 KUKA 型号继续保留各自橙色；iiwa14 的上游材质不受此规则影响。源模型中的部分凸字与壳体共用同一材质，不能独立显示官方照片中的橙色标识，本轮没有制作替代贴图或改动字符几何。[白壳写入记录](quality/materials-kuka-agilus-white.json)保存精确的型号、文件、材质名及前后值。

这三款的独立前端法兰按同份官方图片改为 `#303437` 深色近似，仅修改基色并保留金属度、粗糙度，见[法兰写入记录](quality/materials-kuka-agilus-flanges.json)。底座试验没有采用：原灰色组还包含接口和部分出线，不能独立调整，且照片不足以确认是裸金属还是金属漆；没有为补偿特定灯光下的金属反射而追亮基色。

ROKAE SR3 仅将 `Link3` 已有的窄环材质从饱和蓝调整为 `#B9DDEB` 淡青近似，对照[同型号官方产品图](https://www.rokae.com/en/product/show/726.html?ly=serch)。白壳、金属和其它分区不变，SR5 保留源色。详见[单分区写入记录](quality/materials-rokae-sr3-accent.json)；页面型号全称与源文件短名的差异另见型号复核报告。
