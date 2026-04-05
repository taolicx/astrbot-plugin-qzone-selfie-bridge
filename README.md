# astrbot_plugin_qzone_selfie_bridge

面向 AstrBot 的 QQ 空间自拍联动插件。

这个插件不会重复实现 QQ 空间发布、自拍生成和生活日程三套完整能力，而是把它们串成一条自动发帖流水线：

- 读取 `astrbot_plugin_life_scheduler` 的当天穿搭与日程
- 调用 `astrbot_plugin_gitee_aiimg` 的自拍参考图与改图链路
- 生成与自拍相关的短文案
- 调用 `astrbot_plugin_qzone` 发布带图说说

## 依赖插件

安装并启用以下插件后再使用：

- `astrbot_plugin_life_scheduler`
- `astrbot_plugin_gitee_aiimg`
- `astrbot_plugin_qzone`

## 主要功能

- 手动命令触发自拍发空间
- 自动接管 qzone 的机器人发帖流程
- 支持固定时间自动发自拍说说
- 支持独立的大模型优化自拍生图提示词
- 支持固定角色特征注入，例如性别、气质、年龄感

## 常用命令

- `自拍说说`
- `发自拍说说`
- `自拍空间`
- `自拍发空间`

## 配置说明

插件配置项位于 WebUI 插件配置页，重点包括：

- 是否接管 qzone 发帖
- 是否保留原有配图并追加自拍
- 自拍提示词模板
- 固定角色特征
- 提示词优化器模型
- 自定义每日自动发帖时间

## 说明

- 这个仓库版本已经兼容 AstrBot 通过 GitHub 仓库直接安装
- 如果需要稳定安装，建议优先安装仓库 release 版本
