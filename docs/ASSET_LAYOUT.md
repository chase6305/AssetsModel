# 资源目录与使用约定

仓库只保留一套模型目录和 **67 个 URDF**。默认 URDF 全部使用 GLB；**378 个 DAE 已移除**，原有 **405 个 STL** 作为可选几何资源保留。783 个 GLB 与 STL 按格式放在各模型内部，不再维护顶层 `GLB/` 副本。

```text
RobotModel/<Vendor>/<Model>/
├── <Model>.urdf
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

例如 [UR5e.urdf](../RobotModel/UniversalRobots/UR5e/UR5e.urdf) 现在引用 `Visual/glb/Link1.glb` 和 `Collision/glb/Link1.glb`。复制完整模型目录即可使用，也可以通过导出工具只复制需要的 GLB 与 URDF。全部模型见[目录索引](CATALOG.md)。

| 当前资源 | 数量 | 网格体积 |
| --- | ---: | ---: |
| 默认 GLB | 783 | 311.2 MiB |
| 可选 STL | 405 | 245.3 MiB |
| 合计 | 1,188 | 556.4 MiB |
| DAE | 0 | 0 |

GLB 和 STL 可能表达同一个零件，上表不能用来统计装配的唯一表面数量。默认 GLB 的合计三角形数为 10,407,054。

迁移只改变路径与文件组织：GLB 和保留的 STL 均与迁移前逐文件 SHA-256 一致。URDF 仅更新网格引用，链接、关节、坐标、限位、惯量及颜色定义保持原值。[迁移校验](layout-validation.json)记录了全部模型的三组姿态对比。

## 材质、坐标和命名

GLB 保留 URDF 网格的局部坐标与米制单位。独立 GLB 查看器的默认相机可能与机器人查看器不同；使用配套 URDF 时无需额外旋转网格。

DAE 的颜色、材质分组和角点法线已转入 GLB；Phong 高光采用 PBR 粗糙度近似。STL 本身没有材质信息，主要用于只接受 STL 的工具。碰撞 GLB 省略渲染法线与材质。默认 GLB 场景仅含表面，CAD 辅助线保存在可选第二场景。

历史上同目录同时有 `Link.dae` 和 `Link.stl` 时，两个 GLB 分别命名为 `glb/Link.glb` 和 `glb/Link.stl.glb`，保留各自几何。对应 STL 位于 `stl/Link.stl`。转换工具默认拒绝覆盖内容不同的同名 GLB。

[`glb-manifest.json`](glb-manifest.json) 中 `glb` 和 `retained_stl` 字段记录当前路径，均相对于仓库根目录；`source`、`source_bytes`、`source_sha256` 记录历史转换输入，DAE 的历史路径不再存在。

加载已验证使用 trimesh 4.12.2 / yourdfpy 0.0.60，783 个 GLB 也通过 Khronos 官方格式校验。其他应用的格式支持取决于其实际加载器。转换、导出与校验命令见[工作流程](MESH_WORKFLOW.md)。
