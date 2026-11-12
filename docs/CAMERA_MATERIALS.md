# RealSense 相机外观材质

当前为**待视觉验收的方案草稿**，尚未给出渲染或测试通过结论。`scripts/refine_camera_materials.py` 的 `assetsmodel-camera-materials-v1` 配置针对原有单色相机外壳的平板感，提供柔和的金属反射；USB 插头的石墨色聚合物外观需要显式选择，并单独评审。

| 范围 | 基色 | 金属度 | 粗糙度 | 记录角色 |
| --- | --- | ---: | ---: | --- |
| 6 个相机外壳 | 保留已有线性 `baseColorFactor` | 0.65 | 0.38 | `satin_housing` |
| 4 个可选 USB 插头 | sRGB `#262B30`，转换为线性值后嵌入 | 0.00 | 0.55 | `plug_polymer` |

这些数值是可视化近似，不是厂家实测材质、认证色号或光学参数。已按官方仓库固定提交 `7a87097c499e3b0b69518d54a8e7a9ac1924e87c` 核对各型号描述：[D405](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_d405.urdf.xacro#L19)、[D415](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_d415.urdf.xacro#L18)、[D435](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_d435.urdf.xacro#L19)、[D436](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_d436.urdf.xacro#L19)、[D455](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_d455.urdf.xacro#L40)、[D585](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_d585.urdf.xacro#L19)均有铝制外围机壳的型号说明。D455 引用其型号专属注释，首段沿用的“430”文字不作为型号依据。这些描述支持外壳角色，不提供本配置的金属度或粗糙度。

本库 D405、D415、D455、D585 已嵌入的线性灰色 `[0.5, 0.5, 0.5, 1]` 与上游共享 [`aluminum` 定义](https://github.com/realsenseai/realsense-ros/blob/7a87097c499e3b0b69518d54a8e7a9ac1924e87c/realsense2_description/urdf/_materials.urdf.xacro#L28)一致；D435、D436 保留已有 CAD 材质的 `[0.74902, 0.74902, 0.74902, 1]`。此轮不以新色值覆盖这些来源颜色。

脚本保留已有外壳颜色、材质名称和材质分组，只修改金属度、粗糙度及 `extras.assetsmodel_camera_material` 中的配置与角色。插头仅为原本没有材质绑定的三角形补充一种统一聚合物材质；已有材质不覆盖。此近似没有区分插头内部金属，也没有据 CAD 编号猜测相机光学部件。标量 PBR 调整无法恢复源网格缺失的镜头、前面板图案或品牌细节。

## 固定范围

默认仅处理下列 6 个外壳，路径相对于仓库根目录：

```text
CameraModel/RealSense/RealSense_D405/meshes/glb/d405.glb
CameraModel/RealSense/RealSense_D415/meshes/glb/d415.glb
CameraModel/RealSense/RealSense_D435/meshes/glb/d435.glb
CameraModel/RealSense/RealSense_D436/meshes/glb/d436.glb
CameraModel/RealSense/RealSense_D455/meshes/glb/d455.glb
CameraModel/RealSense/RealSense_D585/meshes/glb/d585.glb
```

添加 `--include-plugs` 才纳入下列 4 个文件：

```text
CameraModel/RealSense/RealSense_D405/meshes/glb/plug.glb
CameraModel/RealSense/RealSense_D415/meshes/glb/plug.glb
CameraModel/RealSense/RealSense_D435/meshes/glb/plug.glb
CameraModel/RealSense/RealSense_D436/meshes/glb/plug.glb
```

`plug_collision.glb`、其它碰撞资源、URDF、STL 和机器人/工具资产都不在修改范围内。BIN 必须逐字节相同；因此顶点、法线、索引和其它嵌入二进制数据保持原样。节点、场景、访问器及 primitive 顺序也保持不变，仅允许上述材质字段与缺失的插头材质绑定变化。带纹理、顶点颜色、蒙皮、动画、变形目标或扩展材质的输入明确拒绝，避免将其静默简化成此配置。

## 预检、备份与写入

工具与独立验证器只需要 Python 标准库。从仓库根目录先生成只读提案：

```bash
python3 scripts/refine_camera_materials.py \
  --report /tmp/camera-materials-proposed.json
```

插头试验在同一命令中添加 `--include-plugs`。跨资产树操作可显式传入 `--root /path/to/AssetsModel`。报告保存逐文件的修改前后 SHA-256、BIN SHA-256 和具体材质变更。

确认方案后，实际写入必须指定与资产根目录互不包含的备份目录：

```bash
python3 scripts/refine_camera_materials.py --apply \
  --backup-dir /tmp/assetsmodel-camera-before-v1 \
  --report /tmp/camera-materials-applied.json
```

是否加入插头应与预检方案一致。脚本先检查全部输入与备份冲突，再备份全部待修改文件并准备替换；不会覆盖已有且内容不同的备份。相同配置再次执行应报告零变更。JSON 报告可放在 `/tmp` 或 `docs/quality/`，禁止写入三类资产目录，符号链接的最终目标同样受检查。

## 独立验证与视觉评审

本轮起始提交为 `a7d0c4d`。独立验证器使用自己的 GLB 容器读取与验收条件，对照该提交逐文件检查 `CameraModel`、`RobotModel`、`ToolModel`，并报告范围外的修改、增加、删除或符号链接：

```bash
python3 scripts/verify_camera_finishes.py \
  --baseline-ref a7d0c4d \
  --json /tmp/camera-materials-independent.json
```

后续新一轮维护应选用该轮**修改前已提交**的基线，不用随时移动的远程分支代替固定起点。几何和字段保真不等于视觉改善；还需依照[预览流程](VISUAL_QUALITY.md)，用相同脚本、视角、灯光和采样参数渲染前后资产，检查正面、背面、轮廓与插头遮挡。当前视觉评审仍待完成。

批量渲染、测试和全库验证应遵守整机资源约定：先检查内存和负载，使用共享排他锁、串行任务和显式线程上限；取锁失败时等待资源空闲，不绕过锁。
