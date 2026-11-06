# 资源目录与使用约定

当前资源包含 **75 个 URDF、12 个机器人品牌、874 个 GLB 和 420 个可选 STL**，统计依据为[本轮库存快照](quality/inventory.json)。默认 URDF 全部使用模型目录内的 GLB，不维护顶层 `GLB/` 副本，运行目录内没有 DAE；迁入模型的原始 DAE/Xacro 输入保存在 `source.tar.gz` 中。2026 年 9 月迁移时移除的 378 个 DAE、当时的 783 个 GLB / 405 个 STL 属于[历史转档记录](GLB_REPORT.md)，不代表当前库存。

```text
RobotModel/<Vendor>/<Model>/
├── <Model>.urdf
├── LICENSE / NOTICE # 有上游许可与声明时随模型保留
├── provenance.json  # 新导入模型的来源、参数与转换记录
├── Visual/
│   ├── glb/          # 默认外观，保留材质和法线
│   └── stl/          # 该模型原本有 STL 外观时保留
└── Collision/
    ├── glb/          # 默认碰撞几何
    └── stl/          # 该模型原本有 STL 碰撞网格时保留

CameraModel/RealSense/<Model>/
├── <Model>.urdf
└── meshes/
    ├── glb/
    └── stl/

ToolModel/Robotiq2F85/
├── Robotiq2F85.urdf
└── meshes/
    ├── glb/
    └── stl/
```

沿用原有 `Visual`、`Collision` 的大小写，`glb`、`stl` 统一小写。没有对应格式资源时不创建空目录；没有从已删除的 DAE 额外生成 STL。

例如 [UR5e.urdf](../RobotModel/UniversalRobots/UR5e/UR5e.urdf) 现在引用 `Visual/glb/Link1.glb` 和 `Collision/glb/Link1.glb`。复制完整模型目录即可使用，也可以通过导出工具复制运行所需的 GLB、URDF，以及模型随附的许可和来源文件。全部模型见[目录索引](CATALOG.md)，新加入的 xArm6、Lite6、iiwa14 和 Gen3 见[新增模型说明](NEW_ROBOTS.md)。

| 当前资源 | 数量 | 网格体积 |
| --- | ---: | ---: |
| 默认 GLB | 874 | 227.7 MiB |
| 可选 STL | 420 | 245.0 MiB |
| 合计 | 1,294 | 472.7 MiB |
| DAE | 0 | 0 |

体积仅统计网格文件，不含 URDF、许可、文档和 Git 历史。精确字节数以库存快照中的 `glb_bytes` / `stl_bytes` 为准；MiB 使用 2²⁰ 字节。GLB 和 STL 可能表达同一个零件，上表不能用来统计装配的唯一表面数量。历史转档的 10,407,054 个三角形也不能直接作为新增型号后的总面数。

2026 年 9 月的目录迁移只改变路径与文件组织：当时的 GLB 和保留 STL 与迁移前逐文件 SHA-256 一致，URDF 只更新网格引用。[历史迁移校验](layout-validation.json)记录了当时 67 个模型的三组姿态对比。本轮随后调整了[品牌材质](BRAND_MATERIALS.md)并加入四款独立机器人；当前文件哈希与历史转档文件不必相同。现有模型的[201 组基线姿态对比](quality/existing-model-comparison.json)与新增模型的[12 组源描述对比](quality/new-robots-independent-audit.json)分别保存验证范围。

## 材质、坐标和命名

GLB 保留 URDF 网格的局部坐标与米制单位。独立 GLB 查看器的默认相机可能与机器人查看器不同；使用配套 URDF 时无需额外旋转网格。

历史 DAE 的颜色、材质分组和角点法线已转入 GLB；Phong 高光采用 PBR 粗糙度近似。本轮按已有分组细调烤漆、塑料、橡胶与金属，品牌颜色是视觉近似，来源与数值见[材质说明](BRAND_MATERIALS.md)。STL 本身没有材质信息，主要用于只接受 STL 的工具；新导入的 STL 外观已将源 URDF 颜色嵌入 GLB。碰撞 GLB 省略渲染法线与材质。默认 GLB 场景仅含表面，存在的 CAD 辅助线保存在可选第二场景。

历史上同目录同时有 `Link.dae` 和 `Link.stl` 时，两个 GLB 分别命名为 `glb/Link.glb` 和 `glb/Link.stl.glb`，保留各自几何。对应 STL 位于 `stl/Link.stl`。转换工具默认拒绝覆盖内容不同的同名 GLB。

[`glb-manifest.json`](glb-manifest.json) 是历史 DAE/STL → GLB 的逐文件转档记录；`glb` 和 `retained_stl` 为转档阶段的资源路径，`source`、`source_bytes`、`source_sha256` 保存历史输入。已移除的 DAE 路径和已替换型号不必仍然存在，不能用该文件判断当前资源总数。当前总量见 [quality/inventory.json](quality/inventory.json)，新增模型的固定提交与输入/输出哈希见各模型内的 `provenance.json`。

迁入前的 [71 个 URDF 的 213 组加载姿态](quality/loader-validation.json)通过检查；迁入前的 [841 个 GLB 的 Khronos 格式校验](quality/glb-format-validation.json)记录为 0 错误、0 警告，另有 23 条提示。格式报告中的逐文件哈希用于确认验证对应的资源版本。其他应用的格式支持取决于其实际加载器。转换、导出与校验命令见[工作流程](MESH_WORKFLOW.md)。

## 从 HumanoidAssets 迁入的模型

- `RobotModel/Franka/Panda/`：`robot.urdf`、`arm.urdf`、`hand.urdf`，与既有 FR3 分开。
- `RobotModel/UniversalRobots/UR5e_Official/robot.urdf`：官方描述版本，保留既有 `UR5e/UR5e.urdf`。

这四份 URDF 只调整网格路径，原关节、动力学、碰撞形状及 mimic 保持不变。
外观 GLB、预览图和原碰撞 STL 保持原字节；碰撞 STL 转 GLB 逐角点检查，无简化。
原 DAE/Xacro/配置、导入记录和迁移前 URDF 收入 `source.tar.gz`，不作为默认运行资源。
每个模型的 `SOURCE.md`、`provenance.json` 和 `validation.json` 记录来源、许可、哈希、
4 组姿态检查及新增 GLB 的格式校验；此前的全库质量报告仍对应其生成时的版本。
