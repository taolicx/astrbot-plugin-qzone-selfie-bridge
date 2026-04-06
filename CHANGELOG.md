# Changelog

## v0.1.11

- 修复新安装后“自拍提示词优化器 API”下拉仍然为空的问题
- 插件启动时除了读取运行中的聊天 Provider，也会在必要时直接从 `cmd_config.json` 回填聊天模型列表
- 在 `initialize`、`on_astrbot_loaded` 和 `on_plugin_loaded` 阶段都会刷新一次配置页下拉，减少首次安装时的时序问题

## v0.1.10

- 新增 `refresh_life_before_publish` 开关
- 开启后每次生成自拍前都会先强制刷新一次 `life_scheduler` 的当日推荐、穿搭和日程
- 强刷失败时会自动回退到当天缓存或原有的缺失补生成逻辑

## v0.1.9

- 定时任务不再只依赖 `event.bot` 才能拿到 QQ 平台 client
- 新增从 AstrBot 平台实例 `get_client()` 回退获取 OneBot client 的逻辑
- 获取到 live client 后会主动绑定回已加载的 qzone 插件 `cfg.client / sender.cfg.client`
- 修复定时发布时“当前没有可用 bot client，无法自动重新登录 QQ 空间”导致无法自动恢复 cookies 的问题

## v0.1.8

- 不再使用 `get_visitor()` 作为 QQ 空间登录预检探针，改为先建立 session 再探测近期动态接口
- `参数错误` 这类非登录类预检异常现在只记警告，不再阻断整条自动发布
- 登录失效仍会继续走原有的清 cookie、刷新 cookie、重试预检与发布重试逻辑

## v0.1.7

- 修复仅安装 `astrbot_plugin_life_scheduler_enhanced` 时，桥接插件在导入阶段仍写死原版模块名导致加载失败的问题
- 改为启动时自动兼容增强版根模块、原版 core 模块和原版平铺模块三种结构
- 配置文件路径和数据目录也会跟随实际检测到的 life_scheduler 插件 ID 自动切换

## v0.1.6

- 在生成图片和文案之前增加 QQ 空间登录预检，避免登录失效时白白浪费一次生成机会
- 预检命中登录失效时，会先清理旧 cookies，再从当前 bot client 拉取新 cookies 并重试一次
- 如果真正发帖阶段才撞到登录失效，也会自动修复登录态并再重试一次发布
- 新增定时发说说结果通知，可把成功图片、文案和失败原因发给指定 QQ 或群聊

## v0.1.5

- 把自拍默认提示词改成明确的“参考图改图”语义，不再使用生图或文生图表述
- 提示词优化器模板同步改成“改图，不是文生图”，强调保留参考图中的同一人物身份一致性
- 更新插件配置页里的相关描述，避免继续误导成生图插件

## v0.1.4

- 生活日程生成失败时自动回退到最近一次可用日程或默认日程
- 避免上游聊天模型短暂网络错误导致定时自拍说说整条失败

## v0.1.3

- 插件启动后自动扫描当前 AstrBot 已配置的聊天 Provider，并回写到优化器下拉选项
- 不再依赖单独修改 AstrBot 后台路由，也能在配置页看到已配置模型

## v0.1.2

- 兼容增强版 `astrbot_plugin_life_scheduler_enhanced`
- 自动识别原版与增强版的配置文件名和数据目录名
- 避免仅安装增强版时出现 `No module named 'astrbot_plugin_life_scheduler'`

## v0.1.1

- 整理为可公开发布的 GitHub 仓库版本
- 更新元数据中的作者、描述与仓库地址
- 补充 README、CHANGELOG 与包标记文件

## v0.1.0

- 初始版本
- 串联 life_scheduler、gitee_aiimg 与 qzone
- 支持自拍图生成、短文案生成与带图发空间
- 支持接管 qzone 原有机器人发帖流程
- 支持固定时间自动发自拍说说
