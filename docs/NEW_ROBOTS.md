# 新增常用机械臂

本轮新增四款独立型号，使用上游真实网格和运动学描述，未通过缩放已有型号构造替代品。默认 URDF 引用模型目录内的 GLB；复制完整目录即可加载，无需 ROS 或 xacro。资源不包含控制器、Gazebo 插件或 transmissions。

| 型号与配置 | 运动自由度 | GLB | 默认 URDF |
| --- | ---: | ---: | --- |
| UFACTORY xArm6，裸臂 | 6 | 14 | [xArm6.urdf](../RobotModel/UFACTORY/xArm6/xArm6.urdf) |
| UFACTORY Lite6，裸臂 | 6 | 14 | [Lite6.urdf](../RobotModel/UFACTORY/Lite6/Lite6.urdf) |
| KUKA LBR iiwa 14 R820，裸臂 | 7 | 16 | [iiwa14.urdf](../RobotModel/KUKA/iiwa14/iiwa14.urdf) |
| Kinova Gen3，7 自由度、无视觉模块、裸臂 | 7 | 16 | [Gen3.urdf](../RobotModel/Kinova/Gen3/Gen3.urdf) |

每款包含 `Visual/glb/`、`Collision/glb/`、`LICENSE`、`NOTICE` 和 `provenance.json`。60 个 GLB 均内嵌资源；外观与碰撞沿用各自上游几何。没有额外添加夹爪、相机或工具。

## 来源与许可

| 模型 | 固定上游版本 | 许可 |
| --- | --- | --- |
| xArm6 / Lite6 | [xarm_ros · cf51617d98bc55a88d9aa735acfa43a0bbb8e425](https://github.com/xArm-Developer/xarm_ros/tree/cf51617d98bc55a88d9aa735acfa43a0bbb8e425) | BSD-3-Clause |
| iiwa14 | [iiwa_stack · 44f9d13c1b444d5dc9fd3e43ba60b7d3b2ea2bbb](https://github.com/IFL-CAMP/iiwa_stack/tree/44f9d13c1b444d5dc9fd3e43ba60b7d3b2ea2bbb) | BSD-2-Clause |
| Gen3 | [ros_kortex · c3280d8a6a6b5d96590d51538e3f265710a6c77d](https://github.com/Kinovarobotics/ros_kortex/tree/c3280d8a6a6b5d96590d51538e3f265710a6c77d) | BSD-3-Clause |

各模型的 `LICENSE` 是对应上游文本的逐字副本，`NOTICE` 列出来源和修改范围。`provenance.json` 保存输入与输出 SHA-256、固定 Git 提交、xacro 参数、转换策略，以及省略的无效源参数。导入时按固定 Git 树的 blob SHA-1 验证实际使用的源文件，拒绝其它提交或修改过的源文件。

Kinova 采用 `GEN3-7DOF-NOVISION_FOR_URDF_ARM_V12.urdf`；没有引入 ros_kortex 中另有许可的 Robotiq 夹爪资源。

## 外观与网格

- xArm6 / Lite6 保留源 CAD 的白壳、灰色关节盖及标识分区，外壳使用金属度 0、粗糙度 0.30；灰色分区使用粗糙度 0.40。没有将整机合并为一种颜色。
- Lite6 的源 DAE 多处使用逐面法线。仅这些平面着色分区按 30° 阈值和局部相邻面扇区重建平滑法线，保留机械硬边；已有平滑源法线保持。Lite6 共处理 36,351 个源面，xArm6 底座处理 808 个源面。所有顶点、三角形与装配尺寸保持原值，未减面或细分。
- iiwa14 的 STL 不携带颜色。按源 URDF 的橙色/灰色分区嵌入材质，将 sRGB 数值转换为线性的 glTF `baseColorFactor`；橙色漆面粗糙度 0.32，灰色分区 0.42。
- Gen3 按源 URDF 灰色外壳嵌入材质并转换色彩空间，粗糙度为 0.38。STL 外观使用 30° 局部扇区平滑法线；碰撞资源保持碰撞用途，不添加外观法线。

PBR 参数用于可视化近似，不表示厂家实测涂层参数。所有 GLB 保存米制局部坐标，URDF 的 mesh origin/scale 沿用上游，不需要查看器额外旋转模型。

## 参数边界

运动关节的名称、父子关系、坐标、轴向、限位及有效惯量均沿用固定版本源描述。以下两处无效源惯量没有被替换为编造数值：

| 型号 | 源数据与处理 |
| --- | --- |
| Lite6 | 固定底座 `link_base` 的惯性矩阵全为零，省略该 `inertial`；原始 XML 保存在 provenance。校验会报告该几何链接缺少惯量。 |
| Gen3 | 固定末端坐标帧 `end_effector_link` 的质量和惯性矩阵全为零，省略该 `inertial`；固定关节 `end_effector` 上未使用的全零 `axis` 同样省略，原 XML 均有记录。该末端帧没有几何。 |

这些文件适合规划和可视化。iiwa 上游描述本身使用 `max_effort=300`、`max_velocity=10` 等通用参数；忠于源描述不等于厂家校准的动力学模型。运行具体动力学仿真时应按实际硬件和任务核实参数。

## 复现导入

仅使用现有模型不需要以下依赖。重新导入需要 Python mesh 依赖和 xacro；本轮使用 xacro 2.1.1：

```bash
python3 -m pip install -r requirements-mesh.txt xacro==2.1.1
```

准备三个上游 Git checkout，并分别切换到上表的完整提交。导入器验证提交和文件内容，输出到新的临时资源根目录：

```bash
python3 scripts/import_common_robots.py \
  --xarm /path/to/xarm_ros \
  --iiwa /path/to/iiwa_stack \
  --kortex /path/to/ros_kortex \
  --models xArm6 Lite6 iiwa14 Gen3 \
  --output /tmp/common-robots-import
```

输出模型先在临时目录完整构建并校验，然后发布；已有非空模型目录会被拒绝覆盖。`--models Lite6` 可以只导入指定型号。`--exported-sources` 用于携带 `.assetsmodel-source.json` 固定 Git 树 blob 清单的源快照；普通 Git checkout 不需要此选项。

默认资源打包工具会同时保留许可证、NOTICE 和 provenance：

```bash
python3 scripts/export_assets.py \
  --models RobotModel/UFACTORY/xArm6 \
  --referenced-only --output /tmp/xarm6-runtime
```

## 独立验证与展示姿态

[独立验证记录](quality/new-robots-independent-audit.json)覆盖四款模型、60 个 GLB 和 12 组合法姿态：逐三角角点比较保持 float32 坐标和 winding；全部运动学链接与源描述、yourdfpy 的姿态矩阵一致；各模型复制到独立目录后加载通过；许可证逐字一致。

使用按 `xarm/`、`iiwa/`、`kortex/` 组织的固定源目录重跑：

```bash
python3 scripts/verify_common_robot_sources.py \
  --source-root /path/to/pinned-sources \
  --json /tmp/new-robots-independent-audit.json
```

xArm6 / Lite6 可用零关节姿态。展示 iiwa14 和 Gen3 的弯臂形态可采用以下合法配置，数值为弧度，其余关节取 0：

```json
{
  "iiwa14": {"iiwa_joint_2": 0.65, "iiwa_joint_4": -1.1, "iiwa_joint_6": 0.8},
  "Gen3": {"joint_2": 0.5, "joint_4": 1.1, "joint_6": -0.6}
}
```

这些是外观展示配置，不是经过碰撞验证的控制轨迹。整体预览与运行记录见 [品牌/网格对比](preview.html)和[预览清单](quality/preview-new-models.json)。
