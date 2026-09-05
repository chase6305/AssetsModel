# 网格优化与校验

默认 URDF 直接位于各模型目录，引用 `Visual/glb/`、`Collision/glb/` 或 `meshes/glb/`。可选 STL 放在相应的 `stl/` 子目录。DAE 和顶层重复的 GLB 目录已移除，具体层级见[资源布局](ASSET_LAYOUT.md)。

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

`compare_asset_trees.py` 默认检查全部 URDF 的三个姿态；传入 `--source /path/to/before` 可额外对比迁移前目录的链接变换、外观/碰撞面数、包围盒及非资源 URDF 属性。

## 导入新的 DAE/STL

DAE 在仓库外作为输入，转换后的文件放入模型的 `glb/` 目录。例如将新外观转入当前模型：

```bash
python3 scripts/convert_glb.py --files /tmp/incoming/Link.dae \
  --output RobotModel/UniversalRobots/UR5e/Visual/glb
```

碰撞资源添加 `--collision`。输出默认拒绝覆盖内容不同的同名 GLB；确认替换对象后才使用 `--overwrite`。工具不自动删除外部输入或修改已有 URDF，新增资源后将相应引用设为 `Visual/glb/Link.glb` 等相对路径。需要保留 STL 时将其放入 `stl/`；不要将 DAE 放回模型库。

转换不进行第二次减面。DAE 场景变换烘焙到各网格的局部坐标；位置、角点法线和材质分组分别处理。每个写回的三角形顶点都与转换前比较。碰撞资源省略法线与材质；STL 外观按 30° 阈值平滑局部顶点扇区，并保留硬边。仅位置及法线均一致时才合并外观顶点。小网格使用 16 位索引，超出范围时使用 32 位索引。

GLB 主场景只包含表面；原 DAE 辅助线保存在可选第二场景。输入目录 DAE/STL 同名时分别输出 `Link.glb` 与 `Link.stl.glb`，避免覆盖。常见的全白顶点颜色作为恒等颜色乘数省略；非白顶点颜色或带纹理的源 DAE 会明确报错。现有 783 个 GLB 均已完成转换和验证。归档的旧格式模型树也可通过显式 `--root` 转换，输出同样使用 `glb/` 子目录。

## 按模型打包

仅导出选定模型实际引用的资源（输出目录须为空）：

```bash
python3 scripts/export_assets.py --models RobotModel/UniversalRobots/UR5e \
  --referenced-only --output /tmp/ur5e-glb-runtime
```

该操作直接复制现有 GLB 与 URDF，保留模型内的格式目录，不带 STL。省略 `--referenced-only` 时保留选定模型的全部 GLB。导出功能只需要标准库。完整转换结果、兼容范围和进一步优化建议见 [GLB 报告](GLB_REPORT.md)。

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

安装可选依赖，并确保 `blender` 在 PATH 中。实际处理环境为 Blender 5.2.1、Python 3.10、trimesh 4.12.2；Blender 工作脚本使用 4.1+ 的法线 API。

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

预览采用实际资产几何、材质与法线，支持当前 GLB URDF。历史减面对比图使用相同相机、光照、尺寸归一化方式和渲染参数。URDF 预览使用零关节位置；它不代表所有关节都位于有效工作姿态。

```bash
python3 scripts/prepare_preview.py RobotModel/UniversalRobots/UR5e/UR5e.urdf /tmp/ur5e-preview
blender -b --factory-startup --threads 4 \
  --python scripts/render_preview.py -- /tmp/ur5e-preview /tmp/ur5e.png
```

也可传入单个 STL/DAE/GLB。预览工具处理网格视觉元素，忽略线段等非表面图元。对比页面见 [preview.html](preview.html)。
