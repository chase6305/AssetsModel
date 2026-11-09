# 网格优化与校验

默认 URDF 直接位于各模型目录，引用 `Visual/glb/`、`Collision/glb/` 或 `meshes/glb/`。可选 STL 放在相应的 `stl/` 子目录。DAE 和顶层重复的 GLB 目录已移除，具体层级见[资源布局](ASSET_LAYOUT.md)。当前[库存快照](quality/inventory.json)为 71 个 URDF、841 个 GLB、404 个可选 STL；新增型号的来源与复现见[新增机器人](NEW_ROBOTS.md)，外观校准见[品牌材质](BRAND_MATERIALS.md)。

## 日常校验

路径、格式目录和 URDF 基础检查只需要 Python 3.10+：

```bash
python3 scripts/validate_assets.py --meshes --layout --json /tmp/assets-validation.json
python3 -m unittest discover -s tests -v
```

`--layout` 要求 URDF 使用 `glb/` 中的网格，STL/GLB 位于各自格式目录，并拒绝新增 DAE。几何和加载检查需要可选依赖：

```bash
python3 -m pip install -r requirements-mesh.txt
python3 scripts/audit_meshes.py --json /tmp/mesh-audit.json
python3 scripts/compare_asset_trees.py --assets . --json /tmp/loader-validation.json
```

完整 glTF 格式检查使用 [Khronos 官方校验器](https://github.com/KhronosGroup/glTF-Validator)，需要 Node.js 22；CI 同样执行此检查。依赖可安装到临时目录：

```bash
npm install --prefix /tmp/assetsmodel-gltf-validation --no-audit --no-fund \
  --ignore-scripts gltf-validator@2.0.0-dev.3.10
NODE_PATH=/tmp/assetsmodel-gltf-validation/node_modules \
  node scripts/validate_glb.cjs --root . --json /tmp/glb-format-validation.json
```

`compare_asset_trees.py` 默认检查全部 URDF 的三个姿态；传入 `--source /path/to/before` 可额外对比基线目录的链接变换、外观/碰撞面数、包围盒及非资源 URDF 属性。目标库新增了型号时添加 `--allow-added-models`：它仍要求基线模型全部存在，只允许目标多出 URDF，并在报告中列出未参与基线对比的新型号。新型号仍需单独运行全库加载和源描述验证。零姿态与 25% / 75% 限位姿态用于装配数值检查，不表示通过碰撞验证的运行轨迹。

本轮保存的证据分别为[71 个模型的加载检查](quality/loader-validation.json)、[原有模型基线对比](quality/existing-model-comparison.json)、[新增模型源描述对比](quality/new-robots-independent-audit.json)和[全库 GLB 格式检查](quality/glb-format-validation.json)。后续更改资源后应重新运行检查，不能仅沿用旧报告。

## GLB 外观维护

现有默认资源是 GLB。[品牌调色](BRAND_MATERIALS.md)修改嵌入材质，保留原材质分区和 BIN；[曲面法线整理](NORMAL_REFINEMENT.md)只处理审查过的曲面或明确局部缺陷；[静态绘制批处理](MESH_BATCHING.md)合并兼容批次并逐场景核对有向角点。已有平滑法线、共面正反两层和机械硬边不能仅按全局计数自动改写。

只读检查当前外观与调色提案：

```bash
python3 scripts/audit_visual_quality.py --json /tmp/visual-audit.json
python3 scripts/refine_materials.py --report /tmp/material-proposals.json
python3 scripts/verify_runtime_visuals.py --json /tmp/runtime-visuals.json
```

调色默认跳过带导入 provenance 的新型号，避免把同品牌不同产品代际套成一种涂装。法线与批处理候选先输出到独立目录，按对应说明复核；若输入在前一步改变，重新生成后续候选，不能用旧 GLB 覆盖新法线或材质。

对比完整采用前快照，可使用仅依赖标准库的独立表面审核：

```bash
python3 scripts/verify_surface_preservation.py \
  --baseline /path/to/before --root . --json /tmp/surface-preservation.json
```

该检查允许法线和材质值改变，严格比较全部场景的有向位置及其它角点属性、表面重数与有效实例变换；原有非机器人外观资产还要求逐文件字节一致。它不替代法线 mask、材质分区或外观图评。`--resume` 只复用输入文件和审核脚本 SHA 均一致的已通过项。

## 导入新的 DAE/STL

DAE 在仓库外作为输入，转换后的文件放入模型的 `glb/` 目录。例如将新外观转入当前模型：

```bash
python3 scripts/convert_glb.py --files /tmp/incoming/Link.dae \
  --output RobotModel/UniversalRobots/UR5e/Visual/glb
```

碰撞资源添加 `--collision`。输出默认拒绝覆盖内容不同的同名 GLB；确认替换对象后才使用 `--overwrite`。工具不自动删除外部输入或修改已有 URDF，新增资源后将相应引用设为 `Visual/glb/Link.glb` 等相对路径。需要保留 STL 时将其放入 `stl/`；不要将 DAE 放回模型库。

转换不进行第二次减面。DAE 场景变换烘焙到各网格的局部坐标；位置、角点法线和材质分组分别处理。每个写回的三角形顶点都与转换前比较。碰撞资源省略法线与材质；STL 外观按 30° 阈值平滑局部顶点扇区，并保留硬边。仅位置及法线均一致时才合并外观顶点。小网格使用 16 位索引，超出范围时使用 32 位索引。

GLB 主场景只包含表面；原 DAE 辅助线保存在可选第二场景。输入目录 DAE/STL 同名时分别输出 `Link.glb` 与 `Link.stl.glb`，避免覆盖。常见的全白顶点颜色作为恒等颜色乘数省略；非白顶点颜色或带纹理的源 DAE 会明确报错。2026 年 9 月转档阶段的 783 个 GLB 与误差数据见[历史转换报告](GLB_REPORT.md)，不代表新增型号后的库存。归档的旧格式模型树也可通过显式 `--root` 转换，输出同样使用 `glb/` 子目录。每次新增或替换网格后都应重跑几何、装配加载与官方格式校验。

## 按模型打包

仅导出选定模型实际引用的资源（输出目录须为空）：

```bash
python3 scripts/export_assets.py --models RobotModel/UniversalRobots/UR5e \
  --referenced-only --output /tmp/ur5e-glb-runtime
```

该操作直接复制现有 GLB 与 URDF，保留模型内的格式目录，不带 STL。存在的 `LICENSE*`、`NOTICE*`、`COPYING*` 和 `provenance.json` 也会随包保留，包括 `--referenced-only` 最小运行包；四款新增型号各有 LICENSE、NOTICE 和 provenance 三个来源文件。输出根目录的 `manifest.json` 记录所复制文件的大小和 SHA-256。省略 `--referenced-only` 时保留选定模型的全部 GLB。导出功能只需要标准库。历史转换结果与兼容范围见 [GLB 报告](GLB_REPORT.md)。

仓库保留原有 URDF 路径和模型目录结构。日常校验只需要 Python 3.10+；减面和预览额外使用本机 Blender 与 Python 几何库。

## 检查内容与目录索引

校验内容包括 XML、资源路径、链接与关节重名、唯一根链接、链接环路、多父链接、mimic 引用与环路、坐标和限位数值、惯性矩阵正定性，以及 STL/GLB 文件容器。输出非零退出码表示错误。CI 自动运行布局、几何、模型加载及官方 GLB 格式检查。

带几何但没有惯量的链接、运动关节的零 effort / velocity 会报告为警告。没有几何的固定坐标帧不要求惯量。`--strict` 可将这些警告视为错误；现有资产并非全部具备动力学参数，因此默认 CI 不启用 strict。此工具检查仓库约定，并不是完整的 URDF 标准或仿真引擎兼容性认证。

更新目录索引：

```bash
python3 scripts/validate_assets.py --layout --markdown docs/CATALOG.md
```

## 减面

以下减面与退化面清理工具面向 STL/DAE 源网格，不直接修改 GLB。目前仓库中它们只会处理保留的 STL；更新 STL 后，需要单独转换并验证对应 GLB。部分 `glb/Link.glb` 来自原 DAE，不能假设同名 STL 含有相同几何和材质。首轮 DAE 减面记录保留在[历史报告](OPTIMIZATION_REPORT.md)。

安装可选依赖，并确保 `blender` 在 PATH 中。历史减面处理环境为 Blender 5.2.1、Python 3.10、trimesh 4.12.2；本轮预览使用 Blender 5.2.2。Blender 工作脚本使用 4.1+ 的法线 API，实际渲染版本记录在输出的 `.render.json` 中。

```bash
python3 -m pip install -r requirements-mesh.txt
```

加载全部网格，进一步检查顶点数值、索引和退化面：

```bash
python3 scripts/audit_meshes.py --json /tmp/mesh-audit.json
```

退化面按几何局部坐标检查并统计；DAE 的 `local_bounds` 不包含场景变换。原始 CAD 的退化面作为质量指标记录，索引或顶点错误会使检查失败。

只清理审计发现的零面积三角形，不对有效表面减面：

```bash
python3 scripts/cleanup_degenerate.py --audit /tmp/mesh-audit.json \
  --apply --backup-dir /tmp/assets-before-cleanup --report /tmp/mesh-cleanup.json
```

该步骤保留有效三角形的坐标和法线。含退化三角形的 DAE 多边形会先按原有三角化方式展开，再移除零面积三角形。

先分析一个模型，默认不会修改资源：

```bash
python3 scripts/optimize_meshes.py RobotModel/AE/AIR10_1210/Collision/stl \
  --report /tmp/mesh-analysis.json
```

写入通过质量检查的结果，必须指定备份目录：

```bash
python3 scripts/optimize_meshes.py RobotModel/AE/AIR10_1210/Collision/stl \
  --apply --backup-dir /tmp/assets-before-optimization \
  --report /tmp/mesh-optimization.json
```

不传模型路径会扫描三个资产目录。路径以仓库根目录为基准。备份保存原目录层次，不同内容不会覆盖旧备份；再次减面请使用新的备份目录。临时备份不应作为长期存档，提交前也可以通过 Git 比较原始资源。

| 设置 | 默认值 | 含义 |
| --- | ---: | --- |
| `--threshold` | 20,000 | 超过此面数才处理 |
| `--visual-target` | 20,000 | 单个外观文件的目标面数 |
| `--collision-target` | 10,000 | 单个碰撞文件的目标面数 |
| `--visual-error` | 0.0005 m | 外观双向采样最大偏差上限 |
| `--collision-error` | 0.00025 m | 碰撞双向采样最大偏差上限 |
| `--samples` | 3,000 | 每种探针、每个方向的采样数 |

目标是期望面数，不是强制上限。每个材质分区按原面数分配预算；较小分区至少保留 100 面的目标预算。默认先尝试目标，再依次提高保留比例到 10%、20%、35%、50%、80%。只采纳第一个通过全部检查的候选；全部失败时仅尝试去除重复面与退化面，否则保留原文件。

外观实际偏差上限还限制在该分区包围盒对角线的 0.1%，碰撞限制在 0.05%。因此小相机和小零件使用的上限比大机器人更严格。

处理原则：

- 精确合并同位置顶点、清除重复面与高度小于 `1e-10` 网格单位的退化面。
- 使用 Blender 的 Collapse 减面，分别处理 DAE 的几何与材质分区，保留材质、单位、场景节点、原变换和辅助线段。
- 以 30° 夹角区分平滑曲面和硬边，重建 DAE 的角点法线；法线去重并压缩索引。STL 只保存面法线，平滑显示取决于加载器。
- 检查连通分量数、边界边和非流形边数量，避免丢失独立零件或引入更多拓扑问题。
- 检查包围盒偏差、面积相对变化（≤1%）；原网格封闭时还检查体积相对变化（≤1%）。
- 双向采样顶点、面中心和表面点，记录最大值、99 分位和 RMS。距离计算先统一尺度，避免小 CAD 三角形受到绝对数值容差影响。
- 只有面数和文件体积同时减少才写入。带 UV、蒙皮、非单位场景缩放或不支持的图元会跳过并报告。

采样偏差不是严格的全表面 Hausdorff 上界，也不保证简化后的碰撞表面完全包住原表面。碰撞网格的减面结果适合在具体规划任务中复核；质量检查不会自动填洞、修补原有非流形结构或补造动力学参数。

## 预览

预览采用实际资产几何、线性 PBR 材质和原始角点法线，支持当前 GLB URDF，以及 URDF 的 box/cylinder/sphere 外观。`--joints` 接受以弧度/米为单位的关节值 JSON；未指定的独立关节取 0，固定与 mimic 关节不可直接赋值。准备器默认检查限位，FR3 和 KR150_R3100_2 等型号不能使用全零展示姿态。

```bash
python3 scripts/prepare_preview.py RobotModel/UniversalRobots/UR5e/UR5e.urdf /tmp/ur5e-preview
blender -b --factory-startup --threads 4 --python-exit-code 1 \
  --python scripts/render_preview.py -- /tmp/ur5e-preview /tmp/ur5e.png \
  --view hero --samples 48 --width 1000 --height 900
```

FR3 带手的有效展示配置示例：

```bash
python3 scripts/prepare_preview.py \
  RobotModel/Franka/FR3/FR3WithHand.urdf /tmp/fr3-preview \
  --joints '{"fr3_joint2":-0.5,"fr3_joint4":-1.8,"fr3_joint6":1.4,"fr3_joint7":0.7,"fr3_finger_joint1":0.03}'
```

代表品牌和新增型号均有包含姿态的预览清单，适合统一相机、光照与采样参数批量生成：

```bash
python3 scripts/render_catalog.py \
  --manifest docs/quality/preview-models.json \
  --output /tmp/brand-previews --work /tmp/brand-preview-packs \
  --samples 48 --width 1000 --height 900
python3 scripts/render_catalog.py \
  --manifest docs/quality/preview-new-models.json \
  --output /tmp/new-robot-previews --work /tmp/new-robot-preview-packs \
  --samples 64 --width 1100 --height 1000
```

`--only kuka-kr10 ur5e` 可限制为对应清单中的模型 ID。历史非法零姿态仅在显式传入 `--allow-outside-limits` 时允许；警告及解析后的全部关节值保存在 `pose.json`。展示配置不表示已通过机器人碰撞检查。

也可传入单个 STL/DAE/GLB；线段等非表面图元不参与预览。批处理使用源文件与脚本指纹判断缓存，并在实际生成 PNG 后记录成功；PNG 旁的 `.render.json` 保存实际渲染参数。色彩、法线、基础形状与不支持的输入范围详见[预览精度](VISUAL_QUALITY.md)，实际对比页见 [preview.html](preview.html)。

## 早期质量检查工具

[2026-11-03 检查](QUALITY_REVIEW.md)记录合批、双面压缩、绕序修复与碰撞审计的原始验证范围。该报告及截图对应当时提交；后续品牌材质、法线与新增模型以当前主分支为准。
