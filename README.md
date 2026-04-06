# astrbot_plugin_qzone_selfie_bridge

面向 AstrBot 的 QQ 空间自拍联动插件。

这个插件不会重复实现 QQ 空间发布、自拍改图和生活日程三套完整能力，而是把它们串成一条自动发说说流水线：

- 读取 `astrbot_plugin_life_scheduler` 或 `astrbot_plugin_life_scheduler_enhanced` 的当天穿搭与日程
- 调用 `astrbot_plugin_gitee_aiimg` 的自拍参考图与改图链路
- 生成与自拍相关的短文案
- 调用 `astrbot_plugin_qzone` 发布带图说说

## 依赖插件

安装并启用以下插件后再使用：

- `astrbot_plugin_life_scheduler` 或 `astrbot_plugin_life_scheduler_enhanced`
- `astrbot_plugin_gitee_aiimg`
- `astrbot_plugin_qzone`

## 主要功能

- 手动命令触发自拍发空间
- 自动接管 qzone 的机器人发帖流程
- 支持固定时间自动发自拍说说
- 支持用独立聊天模型优化自拍改图提示词
- 支持固定角色特征注入，例如性别、年龄感、气质
- 支持在真正出图前先预检 QQ 空间登录状态，必要时自动刷新 cookies 后再继续
- 支持定时结果通知，把成功图片、文案或失败原因发给指定 QQ 或群聊
- 插件启动后自动把当前 AstrBot 已配置的聊天模型写入优化器下拉选项
- 生活日程生成失败时自动回退到最近一次可用日程或默认日程

## 常用命令

- `自拍说说`
- `发自拍说说`
- `自拍空间`
- `自拍发空间`

## 配置说明

插件配置项位于 WebUI 插件配置页，重点包括：

- 是否接管 qzone 发帖
- 是否保留原有配图并追加自拍
- 自拍改图提示词模板
- 固定角色特征
- 提示词优化器模型
- 自定义每日自动发布时间

## 改图说明

- 这个插件的自拍链路是基于参考图的改图，不是纯文生图
- 默认提示词会强调保留同一人物的身份一致性、脸部特征和主体关系
- 上游优化器在润色提示词时，也会明确按“改图，不是文生图”的方式处理

## 说明

- 这个仓库版本已经兼容 AstrBot 通过 GitHub 仓库直接安装
- 如需稳定安装，建议优先使用 release 版本
- 插件会自动兼容 `astrbot_plugin_life_scheduler` 和 `astrbot_plugin_life_scheduler_enhanced`，并按实际安装版本切换配置文件与数据目录
