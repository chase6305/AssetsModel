# 外观网格与预览精度

预览准备器直接读取 GLB 的默认场景、节点变换、三角形分组、原始 `NORMAL` 和 PBR 材质。`baseColorFactor` 使用 glTF 线性色彩值；不再通过 trimesh 的 8 位 `main_color` 量化，也不再把各表面的金属度和粗糙度统一为一个值。外观材质优先采用 GLB 内的分区；仅无内置材质的表面使用显式 URDF 颜色作为回退值。独立 GLB 或没有显式 URDF 颜色的表面使用 [glTF 默认材质](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#default-material)：白色、金属度 1、粗糙度 1、单面。

各 part 的 `scene.json` 字段包含 `color`、`metallic`、`roughness`、`alpha_mode`、`alpha_cutoff`、`double_sided`。`.npz` 包含模型装配后的 `vertices`、`faces` 与逐角点 `normals`。法线使用逆转置对应的行向量变换，非均匀缩放后归一化；镜像变换同步反转三角形绕序，避免表面朝向翻转。GLB 可选 CAD 辅助线场景不参与模型预览。

当前机器人外观 GLB 的材质均为不透明材质，且没有贴图。若输入含贴图、顶点颜色、蒙皮或动画，准备器会明确报错，避免输出丢失这些外观信息的评审图。尚不支持纹理导出，也不声称支持完整 glTF 渲染规范。标量 `alpha_cutoff` 已由渲染脚本用于 glTF `MASK` 的 0/1 截断；现有不透明机器人材质不使用该分支。

URDF 的 `box`、`cylinder`、`sphere` 外观也可准备。基础形状在本地按给定尺寸生成预览三角面，再应用视觉元素原点和链接姿态；盒体与圆柱端盖保持平面法线，圆柱侧壁与球面使用解析平滑法线。这些三角面仅用于展示，不会写回 URDF 或替换模型碰撞几何。

## 关节姿态

默认关节值为 0，但须处于 URDF 限位内。`--joints` 使用 JSON 对象，旋转关节单位为弧度，直线关节单位为米；未指定的独立关节仍为 0。工具按 `parent × origin × axis motion` 计算各链接坐标，按 `multiplier` / `offset` 递归处理 mimic 关节，不允许直接指定固定或 mimic 关节。

Franka FR3 的 joint4 与 joint6 的限位不包含 0。使用显式展示姿态：

```bash
python3 scripts/prepare_preview.py \
  RobotModel/Franka/FR3/FR3WithHand.urdf /tmp/fr3-preview \
  --joints '{"fr3_joint2":-0.5,"fr3_joint4":-1.8,"fr3_joint6":1.4,"fr3_joint7":0.7,"fr3_finger_joint1":0.03}'
```

此姿态用于外观评审，不是碰撞检查后的可执行机器人指令。KUKA KR150_R3100_2 的 joint2 同样不包含零位，准备全库预览时也需要配置有效姿态。历史零姿态对比可显式使用 `--allow-outside-limits`；工具会保留超限警告并记录在 `pose.json`。每个预览包都附带实际已解析的关节值，含 mimic 关节。

## 可复现渲染

安装仓库的可选网格依赖并确保 Blender 在 `PATH` 中。准备好上述 FR3 预览包后，可直接渲染：

```bash
blender -b --factory-startup --threads 4 --python-exit-code 1 \
  --python scripts/render_preview.py -- /tmp/fr3-preview /tmp/fr3-preview.png \
  --view hero --samples 48 --width 1000 --height 900
```

默认使用深灰地面、固定面积光源、EEVEE 原生单面剔除与 AgX 色彩管理；`--engine cycles` 可显式选择路径追踪工作室近似，限制见下文。`--theme light` 可切换浅色地面。`--view` 可选择 `hero`、`front`、`right`、`rear`、`top`，正交相机按实际宽高比自动容纳模型。输出 PNG 旁的 `.render.json` 保存视角、尺寸、采样数、Blender 版本和模型包围盒。材质对比应使用同一套渲染脚本与参数；不同光照或曝光下的亮度差不能作为材质修改的证据。

使用包含关节姿态的代表品牌清单批量渲染：

```bash
python3 scripts/render_catalog.py \
  --manifest docs/quality/preview-models.json \
  --output /tmp/assetsmodel-brand-previews --work /tmp/assetsmodel-brand-packs \
  --samples 48 --width 1000 --height 900
```

`--only kuka-kr10 ur5e` 可限制为清单中的指定 ID。缓存指纹包含模型 URDF、所引用网格、姿态、渲染设置及相关脚本；修改这些输入后会重新渲染。Blender 脚本异常以非零退出码终止，只有实际生成 PNG 后才写入成功缓存。批处理在完成后生成 `manifest.json`；额外安装 Pillow 时还会生成联系表。对比旧资产树时可用 `--root /path/to/baseline`，仍应使用同一份脚本与清单。

## 只读质量审计

```bash
python3 scripts/audit_visual_quality.py --json /tmp/visual-audit.json
python3 -m unittest discover -s tests -p test_preview.py -v
```

审计读取机器人外观 GLB，报告法线缺失、零法线、单位长度偏差、与三角形朝向相反的角点、退化三角形、PBR 数值范围和贴图数量。`--all-categories` 可额外检查相机和工具；不会修改资源。非有限值、非法索引和非法材质数值使审计失败；原 CAD 的退化面与法线方向作为待复核的质量指标保留。相反朝向的角点可能来自源 CAD 的绕序或平滑，不依据该计数自动重算法线。

回归覆盖精确线性 PBR、默认场景选择、默认材质与 URDF 颜色回退、嵌套 GLB TRS、原始法线、非均匀镜像变换、基础形状尺寸及法线、关节局部轴运动、连续 mimic 链和输入限位。FR3 的非零姿态还与 `yourdfpy` 的全部链接坐标逐一比较。

## 单面材质与渲染引擎

默认 `--engine eevee` 使用 EEVEE 原生背面剔除，适合检查 glTF 的 `doubleSided=false` 及 CAD 共面的正反两层。`--engine cycles` 是带阴影和间接光的工作室预览；Cycles 会把单面材质按双面参与渲染，因此不能以其共面异色表面的像素差来判断资产是否改变。Blender 的 glTF 文档将背面剔除列为 [EEVEE 材质设置](https://docs.blender.org/manual/it/5.2/addons/scene_gltf2.html)。

本次用四种面/对象排列的最小场景做了实测：Cycles 中仅切换 `use_backface_culling` 不改变背面；使用透明背面节点会让共面前后层穿孔。因此本流程保留源面，以默认 EEVEE 验收这类资产。不要把背面透明节点当作 CAD 共面层的修复。

标量 `alphaMode=MASK` 已按 `alpha_cutoff` 转为 0/1；当前资产没有纹理，带纹理的输入仍明确拒绝。每个 PNG 的 `.render.json` 记录实际引擎和单面处理方式。
