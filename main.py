from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
import zoneinfo
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image as CoreImage, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.star import StarMetadata, star_registry
from astrbot.core.star.star_tools import StarTools

# AstrBot 加载单个插件时，不一定会把整个 plugins 根目录加入 sys.path。
# 这里显式注入同级插件目录，确保桥接插件能导入已安装的 sibling plugins。
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_gitee_aiimg.core.edit_router import EditRouter
from astrbot_plugin_gitee_aiimg.core.image_format import decode_base64_image_payload
from astrbot_plugin_gitee_aiimg.core.image_manager import ImageManager
from astrbot_plugin_gitee_aiimg.core.provider_registry import ProviderRegistry
from astrbot_plugin_gitee_aiimg.core.ref_store import ReferenceStore
from astrbot_plugin_gitee_aiimg.core.utils import close_session, get_images_from_event
from astrbot_plugin_qzone.core.model import Post
from astrbot_plugin_qzone.core.qzone.api import QzoneAPI
from astrbot_plugin_qzone.core.qzone.session import QzoneSession
from astrbot_plugin_qzone.core.qzone.utils import download_file as download_remote_image

LIFE_PLUGIN_ID = ""

try:
    # 优先兼容增强版。它在 GitHub 安装场景下通常使用根模块结构。
    from astrbot_plugin_life_scheduler_enhanced.data import (
        ScheduleData,
        ScheduleDataManager,
    )
    from astrbot_plugin_life_scheduler_enhanced.generator import SchedulerGenerator

    LIFE_PLUGIN_ID = "astrbot_plugin_life_scheduler_enhanced"
except ModuleNotFoundError:
    try:
        # 兼容原版 life_scheduler 的 core 结构。
        from astrbot_plugin_life_scheduler.core.data import (
            ScheduleData,
            ScheduleDataManager,
        )
        from astrbot_plugin_life_scheduler.core.generator import SchedulerGenerator

        LIFE_PLUGIN_ID = "astrbot_plugin_life_scheduler"
    except ModuleNotFoundError:
        # 某些打包方式会把原版 life_scheduler 平铺到根目录。
        from astrbot_plugin_life_scheduler.data import ScheduleData, ScheduleDataManager
        from astrbot_plugin_life_scheduler.generator import SchedulerGenerator

        LIFE_PLUGIN_ID = "astrbot_plugin_life_scheduler"


@dataclass(slots=True)
class BridgeConfig:
    send_preview_to_chat: bool
    regenerate_life_when_missing: bool
    takeover_qzone_publish: bool
    append_selfie_to_existing_images: bool
    custom_publish_enabled: bool
    custom_publish_times: tuple[str, ...]
    precheck_qzone_before_publish: bool
    auto_refresh_qzone_cookies: bool
    notify_target_users: tuple[str, ...]
    notify_target_groups: tuple[str, ...]
    notify_on_success: bool
    notify_on_failure: bool
    selfie_prompt_template: str
    selfie_character_traits: str
    optimize_selfie_prompt: bool
    selfie_prompt_optimizer_provider_id: str
    selfie_prompt_optimizer_template: str
    caption_prompt_template: str
    fallback_caption_template: str

    @staticmethod
    def _normalize_time_items(raw: Any) -> tuple[str, ...]:
        if isinstance(raw, str):
            parts = re.split(r"[\s,\uff0c;\uff1b|]+", raw.strip())
        elif isinstance(raw, list):
            parts = [str(item).strip() for item in raw]
        else:
            return ()

        result: list[str] = []
        for item in parts:
            if not item or item in result:
                continue
            result.append(item)
        return tuple(result)

    @staticmethod
    def _normalize_id_items(raw: Any) -> tuple[str, ...]:
        if isinstance(raw, str):
            parts = re.split(r"[\s,\uff0c;\uff1b|]+", raw.strip())
        elif isinstance(raw, list):
            parts = [str(item).strip() for item in raw]
        else:
            return ()

        result: list[str] = []
        for item in parts:
            if not item or not item.isdigit() or item in result:
                continue
            result.append(item)
        return tuple(result)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "BridgeConfig":
        data = raw or {}
        return cls(
            send_preview_to_chat=bool(data.get("send_preview_to_chat", True)),
            regenerate_life_when_missing=bool(
                data.get("regenerate_life_when_missing", True)
            ),
            takeover_qzone_publish=bool(data.get("takeover_qzone_publish", True)),
            append_selfie_to_existing_images=bool(
                data.get("append_selfie_to_existing_images", True)
            ),
            custom_publish_enabled=bool(data.get("custom_publish_enabled", False)),
            custom_publish_times=cls._normalize_time_items(
                data.get("custom_publish_times", [])
            ),
            precheck_qzone_before_publish=bool(
                data.get("precheck_qzone_before_publish", True)
            ),
            auto_refresh_qzone_cookies=bool(
                data.get("auto_refresh_qzone_cookies", True)
            ),
            notify_target_users=cls._normalize_id_items(
                data.get("notify_target_users", [])
            ),
            notify_target_groups=cls._normalize_id_items(
                data.get("notify_target_groups", [])
            ),
            notify_on_success=bool(data.get("notify_on_success", True)),
            notify_on_failure=bool(data.get("notify_on_failure", True)),
            selfie_prompt_template=str(
                data.get("selfie_prompt_template")
                or (
                    "请基于提供的自拍参考图完成一次自然、真实、生活感强的自拍改图。"
                    "必须保持同一人物的身份一致性、脸部特征和主体关系。"
                    "穿搭风格：{outfit_style}。"
                    "今日穿搭：{outfit}。"
                    "{character_traits_block}"
                    "请重点调整穿搭、发型细节、表情、姿态、背景氛围与镜头质感。"
                    "整体效果要像本人随手拍下的真实生活自拍，不要变成陌生人，也不要做成纯文生图感。"
                    "{extra}"
                )
            ),
            selfie_character_traits=str(data.get("selfie_character_traits") or "").strip(),
            optimize_selfie_prompt=bool(data.get("optimize_selfie_prompt", False)),
            selfie_prompt_optimizer_provider_id=str(
                data.get("selfie_prompt_optimizer_provider_id") or ""
            ).strip(),
            selfie_prompt_optimizer_template=str(
                data.get("selfie_prompt_optimizer_template")
                or (
                    "\u4f60\u8981\u628a\u4ee5\u4e0b\u81ea\u62cd\u6539\u56fe\u63d0\u793a\u8bcd\u4f18\u5316\u6210\u66f4\u9002\u5408\u53c2\u8003\u56fe\u6539\u56fe\u6a21\u578b\u7684\u7248\u672c\u3002"
                    "\u8fd9\u662f\u6539\u56fe\uff0c\u4e0d\u662f\u6587\u751f\u56fe\uff0c\u5fc5\u987b\u56f4\u7ed5\u53c2\u8003\u56fe\u4e2d\u7684\u540c\u4e00\u4eba\u7269\u505a\u7f16\u8f91\u3002"
                    "\u8bf7\u53ea\u8f93\u51fa\u4f18\u5316\u540e\u7684\u63d0\u793a\u8bcd\u672c\u8eab\uff0c\u4e0d\u8981\u89e3\u91ca\uff0c\u4e0d\u8981\u5206\u70b9\uff0c\u4e0d\u8981\u5e26\u5f15\u53f7\u3002"
                    "\u76ee\u6807\uff1a\u771f\u5b9e\u3001\u81ea\u7136\u3001\u597d\u770b\u3001\u751f\u6d3b\u611f\u5f3a\u7684\u81ea\u62cd\u6539\u56fe\uff0c\u4eba\u50cf\u81ea\u7136\uff0c\u7a7f\u642d\u6e05\u6670\uff0c\u6784\u56fe\u5e72\u51c0\uff0c\u7167\u7247\u8d28\u611f\u597d\u3002"
                    "\u4f18\u5148\u4fdd\u7559\u4eba\u7269\u7684\u8eab\u4efd\u4e00\u81f4\u6027\uff0c\u7a81\u51fa\u53d1\u578b\u3001\u4e94\u5b98\u3001\u8868\u60c5\u3001\u7a7f\u642d\u548c\u6574\u4f53\u6c14\u8d28\u7684\u7edf\u4e00\u6027\uff0c\u53ef\u4ee5\u8c03\u6574\u80cc\u666f\u6c1b\u56f4\u4f46\u4e0d\u8981\u504f\u79bb\u771f\u5b9e\u81ea\u62cd\u611f\u3002"
                    "\u57fa\u7840\u63d0\u793a\u8bcd\uff1a{base_prompt}\u3002"
                    "\u7a7f\u642d\u98ce\u683c\uff1a{outfit_style}\u3002"
                    "\u4eca\u65e5\u7a7f\u642d\uff1a{outfit}\u3002"
                    "{character_traits_block}"
                    "\u9644\u52a0\u8981\u6c42\uff1a{extra}\u3002"
                )
            ),
            caption_prompt_template=str(
                data.get("caption_prompt_template")
                or (
                    "你要为一条配有自拍的QQ空间说说写文案。"
                    "请根据以下信息，用第一人称写一段自然、生活化、像真人发的说说文案。"
                    "穿搭风格：{outfit_style}。"
                    "今日穿搭：{outfit}。"
                    "今日安排：{schedule}。"
                    "自拍设定：{selfie_prompt}。"
                    "附加要求：{extra}。"
                    "要求：80字以内，不要分点，不要解释，不要带引号，不要写成提示词。"
                )
            ),
            fallback_caption_template=str(
                data.get("fallback_caption_template")
                or "今天是{outfit_style}的一天，换上这身衣服出门前随手拍了一张。{schedule}"
            ),
        )


class QzoneRuntimeConfig:
    """给 qzone 底层 session/api 提供最小配置面。"""

    def __init__(self, raw: dict[str, Any], config_path: Path):
        self.raw = raw
        self.config_path = config_path
        self.cookies_str = str(raw.get("cookies_str") or "").strip()
        self.timeout = int(raw.get("timeout") or 10)
        self.client = None

    def update_cookies(self, cookies_str: str):
        self.cookies_str = cookies_str
        self.raw["cookies_str"] = cookies_str
        self.config_path.write_text(
            json.dumps(self.raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class DailySelfiePublishScheduler:
    """Schedule fixed daily selfie-post jobs for the bridge plugin."""

    def __init__(
        self,
        plugin: "QzoneSelfieBridgePlugin",
        *,
        timezone: zoneinfo.ZoneInfo,
        time_specs: tuple[str, ...],
    ) -> None:
        self.plugin = plugin
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.time_specs = time_specs

    def start(self) -> None:
        for time_spec in self.time_specs:
            hour, minute, second, normalized = self.plugin._parse_daily_time_spec(
                time_spec
            )
            self.scheduler.add_job(
                self.plugin._run_custom_publish_job,
                trigger=CronTrigger(
                    hour=hour,
                    minute=minute,
                    second=second,
                    timezone=self.timezone,
                ),
                args=[normalized],
                id=f"qzone_selfie_bridge_daily_{normalized}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.start()

    async def terminate(self) -> None:
        self.scheduler.remove_all_jobs()
        try:
            self.scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning(
                "[QzoneSelfieBridge] custom publish scheduler shutdown failed: %s",
                exc,
            )


class QzoneSelfieBridgePlugin(Star):
    """把生活日程、自拍生成和 QQ 空间发布串成一条流水线。"""

    DEFAULT_ORIGIN = "plugin:qzone_selfie_bridge"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = BridgeConfig.from_mapping(dict(config))
        self.data_dir = Path(
            str(StarTools.get_data_dir("astrbot_plugin_qzone_selfie_bridge"))
        )

        plugins_dir = Path(__file__).resolve().parent.parent
        self.astrbot_data_dir = plugins_dir.parent
        self.config_dir = self.astrbot_data_dir / "config"
        self.plugin_data_root = self.astrbot_data_dir / "plugin_data"

        self._publish_lock = asyncio.Lock()
        self._patched_qzone_services: dict[int, tuple[Any, Callable[..., Awaitable[Post]]]] = {}
        self._schedule_timezone = self._resolve_schedule_timezone()
        self._custom_publish_scheduler: DailySelfiePublishScheduler | None = None

    async def initialize(self):
        self.life_plugin_id = LIFE_PLUGIN_ID
        self.life_config_path = self.config_dir / f"{self.life_plugin_id}_config.json"
        self.qzone_config_path = self.config_dir / "astrbot_plugin_qzone_config.json"
        self.gitee_config_path = (
            self.config_dir / "astrbot_plugin_gitee_aiimg_config.json"
        )

        self.life_config_raw = self._read_json(self.life_config_path)
        self.qzone_config_raw = self._read_json(self.qzone_config_path)
        self.gitee_config_raw = self._read_json(self.gitee_config_path)

        self.life_data_dir = self.plugin_data_root / self.life_plugin_id
        self.life_data_dir.mkdir(parents=True, exist_ok=True)
        self.life_data_mgr = ScheduleDataManager(
            self.life_data_dir / "schedule_data.json"
        )
        self.life_generator = SchedulerGenerator(
            self.context, self.life_config_raw, self.life_data_mgr
        )
        logger.info(
            "[QzoneSelfieBridge] use life scheduler plugin id=%s config=%s data=%s",
            self.life_plugin_id,
            self.life_config_path,
            self.life_data_dir,
        )

        self.gitee_data_dir = self.plugin_data_root / "astrbot_plugin_gitee_aiimg"
        self.gitee_data_dir.mkdir(parents=True, exist_ok=True)
        self.imgr = ImageManager(self.gitee_config_raw, self.gitee_data_dir)
        self.registry = ProviderRegistry(
            self.gitee_config_raw,
            imgr=self.imgr,
            data_dir=self.gitee_data_dir,
        )
        self.edit = EditRouter(
            self.gitee_config_raw,
            self.imgr,
            self.gitee_data_dir,
            registry=self.registry,
        )
        self.refs = ReferenceStore(self.gitee_data_dir)

        self._patch_qzone_publishers()
        self._start_custom_publish_scheduler()

    async def terminate(self):
        await self._stop_custom_publish_scheduler()
        self._unpatch_qzone_publishers()

        try:
            await self.edit.close()
        except Exception as exc:
            logger.warning("[QzoneSelfieBridge] close edit router failed: %s", exc)
        try:
            await self.imgr.close()
        except Exception as exc:
            logger.warning("[QzoneSelfieBridge] close image manager failed: %s", exc)
        try:
            await close_session()
        except Exception:
            pass

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._patch_qzone_publishers()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: StarMetadata):
        self._patch_qzone_publishers(metadata.star_cls if metadata else None)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "自拍说说",
        alias={"发自拍说说", "自拍空间", "自拍发空间"},
    )
    async def publish_selfie_qzone(
        self, event: AstrMessageEvent, extra: str | None = None
    ):
        """手动生成自拍并发布到 QQ 空间。"""
        extra = (extra or event.message_str.partition(" ")[2]).strip() or None

        if self._publish_lock.locked():
            yield event.plain_result("已有一条自拍说说任务在执行，稍后再试。")
            return

        yield event.plain_result("正在生成自拍并准备发布说说...")
        try:
            post, caption, image_path = await self.publish_selfie_post(
                extra=extra,
                event=event,
                origin=event.unified_msg_origin or self.DEFAULT_ORIGIN,
            )
            if self.config.send_preview_to_chat:
                await event.send(event.image_result(str(image_path)))
            yield event.plain_result(
                f"发布成功，tid={post.tid or 'unknown'}\n文案：{caption}"
            )
        except Exception as exc:
            logger.error("[QzoneSelfieBridge] publish failed: %s", exc, exc_info=True)
            yield event.plain_result(f"发布失败：{exc}")

    async def publish_selfie_post(
        self,
        *,
        extra: str | None = None,
        event: AstrMessageEvent | None = None,
        origin: str | None = None,
        original_images: list[Any] | None = None,
        service: Any | None = None,
    ) -> tuple[Post, str, Path]:
        """统一发布入口，供手动命令和 qzone 接管流程复用。"""
        async with self._publish_lock:
            await self._ensure_qzone_publish_ready(event=event, origin=origin)
            (
                caption,
                publish_images,
                preview_images,
                image_path,
            ) = await self._build_selfie_publish_bundle(
                extra=extra,
                event=event,
                origin=origin,
                original_images=original_images,
            )

            qzone_service = service or self._find_qzone_service()
            if qzone_service is not None:
                post = await self._publish_via_service(
                    qzone_service,
                    event=event,
                    origin=origin,
                    caption=caption,
                    publish_images=publish_images,
                    preview_images=preview_images,
                )
            else:
                post = await self._publish_direct_to_qzone(
                    event=event,
                    origin=origin,
                    caption=caption,
                    publish_images=publish_images,
                    preview_images=preview_images,
                )

            return post, caption, image_path

    def _resolve_schedule_timezone(self) -> zoneinfo.ZoneInfo:
        timezone_name = self.context.get_config().get("timezone")
        try:
            return zoneinfo.ZoneInfo(timezone_name or "Asia/Shanghai")
        except Exception:
            logger.warning(
                "[QzoneSelfieBridge] invalid timezone=%s, fallback to Asia/Shanghai",
                timezone_name,
            )
            return zoneinfo.ZoneInfo("Asia/Shanghai")

    @staticmethod
    def _parse_daily_time_spec(time_spec: str) -> tuple[int, int, int, str]:
        text = (time_spec or "").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if not match:
            raise ValueError(
                f"invalid time '{time_spec}', expected HH:MM or HH:MM:SS"
            )

        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError(
                f"invalid time '{time_spec}', hour/minute/second out of range"
            )
        normalized = f"{hour:02d}:{minute:02d}" + (
            f":{second:02d}" if second else ""
        )
        return hour, minute, second, normalized

    def _iter_valid_custom_publish_times(self) -> tuple[str, ...]:
        valid: list[str] = []
        for time_spec in self.config.custom_publish_times:
            try:
                _hour, _minute, _second, normalized = self._parse_daily_time_spec(
                    time_spec
                )
            except ValueError as exc:
                logger.warning(
                    "[QzoneSelfieBridge] skip invalid custom publish time=%s error=%s",
                    time_spec,
                    exc,
                )
                continue
            if normalized not in valid:
                valid.append(normalized)
        return tuple(valid)

    def _start_custom_publish_scheduler(self) -> None:
        if self._custom_publish_scheduler is not None:
            return
        if not self.config.custom_publish_enabled:
            return

        time_specs = self._iter_valid_custom_publish_times()
        if not time_specs:
            logger.warning(
                "[QzoneSelfieBridge] custom publish enabled but no valid times configured"
            )
            return

        scheduler = DailySelfiePublishScheduler(
            self,
            timezone=self._schedule_timezone,
            time_specs=time_specs,
        )
        scheduler.start()
        self._custom_publish_scheduler = scheduler
        logger.info(
            "[QzoneSelfieBridge] custom publish scheduler started: timezone=%s times=%s",
            self._schedule_timezone.key,
            ", ".join(time_specs),
        )

    async def _stop_custom_publish_scheduler(self) -> None:
        if self._custom_publish_scheduler is None:
            return
        await self._custom_publish_scheduler.terminate()
        self._custom_publish_scheduler = None

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"缺少配置文件：{path}")
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def _iter_qzone_plugins(self) -> Iterable[Any]:
        for metadata in star_registry:
            star_obj = getattr(metadata, "star_cls", None)
            if star_obj is None or star_obj is self:
                continue
            module_name = star_obj.__class__.__module__
            if module_name == "astrbot_plugin_qzone.main" or module_name.startswith(
                "astrbot_plugin_qzone."
            ):
                yield star_obj

    def _find_qzone_service(self) -> Any | None:
        for plugin in self._iter_qzone_plugins():
            service = getattr(plugin, "service", None)
            if service is not None:
                return service
        return None

    def _find_qzone_sender(self) -> Any | None:
        for plugin in self._iter_qzone_plugins():
            sender = getattr(plugin, "sender", None)
            if sender is not None:
                return sender
        return None

    def _iter_platform_clients(self) -> Iterable[Any]:
        platform_manager = getattr(self.context, "platform_manager", None)
        platform_insts = getattr(platform_manager, "platform_insts", None)
        if not isinstance(platform_insts, list):
            return

        for platform in platform_insts:
            get_client = getattr(platform, "get_client", None)
            if not callable(get_client):
                continue
            try:
                client = get_client()
            except Exception as exc:
                logger.warning(
                    "[QzoneSelfieBridge] get platform client failed: %s",
                    exc,
                )
                continue
            if client is not None:
                yield client

    async def _bind_qzone_client(self, client: Any | None) -> Any | None:
        if client is None:
            return None

        bound = False
        for plugin in self._iter_qzone_plugins():
            cfg = getattr(plugin, "cfg", None)
            if cfg is not None and getattr(cfg, "client", None) is None:
                setattr(cfg, "client", client)
                bound = True

            sender = getattr(plugin, "sender", None)
            sender_cfg = getattr(sender, "cfg", None)
            if sender_cfg is not None and getattr(sender_cfg, "client", None) is None:
                setattr(sender_cfg, "client", client)
                bound = True

        if bound:
            logger.info("[QzoneSelfieBridge] bound live bot client into qzone plugin")
        return client

    def _find_qzone_client(self, event: AstrMessageEvent | None = None) -> Any | None:
        client = getattr(event, "bot", None)
        if client is not None:
            return client

        sender = self._find_qzone_sender()
        client = getattr(getattr(sender, "cfg", None), "client", None)
        if client is not None:
            return client

        for plugin in self._iter_qzone_plugins():
            client = getattr(getattr(plugin, "cfg", None), "client", None)
            if client is not None:
                return client
        for client in self._iter_platform_clients():
            return client
        return None

    async def _sync_live_qzone_cookies(self, cookies_str: str | None = None) -> None:
        seen_sessions: set[int] = set()
        for plugin in self._iter_qzone_plugins():
            cfg = getattr(plugin, "cfg", None)
            if cookies_str is not None and cfg is not None and hasattr(cfg, "update_cookies"):
                try:
                    cfg.update_cookies(cookies_str)
                except Exception as exc:
                    logger.warning(
                        "[QzoneSelfieBridge] sync qzone plugin cookies failed: %s",
                        exc,
                    )

            for session in (
                getattr(plugin, "session", None),
                getattr(getattr(plugin, "service", None), "session", None),
            ):
                if session is None or id(session) in seen_sessions:
                    continue
                seen_sessions.add(id(session))
                if hasattr(session, "invalidate"):
                    try:
                        await session.invalidate()
                    except Exception as exc:
                        logger.warning(
                            "[QzoneSelfieBridge] invalidate qzone session failed: %s",
                            exc,
                        )

    @staticmethod
    def _looks_like_qzone_login_error(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        keywords = ("登录", "失效", "cookie", "skey", "g_tk", "expired", "-100")
        return any(keyword in text for keyword in keywords)

    async def _refresh_qzone_runtime_cookies(
        self, qzone_cfg: QzoneRuntimeConfig
    ) -> str:
        if qzone_cfg.client is None:
            raise RuntimeError("当前没有可用 bot client，无法自动刷新 QQ 空间 cookies")

        qzone_cfg.update_cookies("")
        session = QzoneSession(qzone_cfg)
        await session.invalidate()
        await session.login(None)
        await self._sync_live_qzone_cookies(qzone_cfg.cookies_str)
        logger.info("[QzoneSelfieBridge] refreshed qzone cookies from bot client")
        return qzone_cfg.cookies_str

    async def _probe_qzone_ready(self, qzone_cfg: QzoneRuntimeConfig) -> None:
        session = QzoneSession(qzone_cfg)
        api = QzoneAPI(session, qzone_cfg)
        try:
            # 预检只想识别“登录态是否明显失效”，不应该因为 visitor 接口自身参数问题误判失败。
            # 先走 session.get_ctx() 确认 cookies / uin / gtk 能正常建立，再用近期动态接口做轻量探测。
            await session.get_ctx()
            resp = await api.get_recent_feeds(page=1)
            if not resp.ok:
                detail = str(resp.message or resp.code)
                if self._looks_like_qzone_login_error(detail):
                    raise RuntimeError(detail)
                logger.warning(
                    "[QzoneSelfieBridge] qzone precheck got non-login error, continue anyway: %s",
                    detail,
                )
        finally:
            await api.close()

    async def _ensure_qzone_publish_ready(
        self,
        *,
        event: AstrMessageEvent | None = None,
        origin: str | None = None,
    ) -> None:
        if not self.config.precheck_qzone_before_publish:
            return

        self.qzone_config_raw = self._read_json(self.qzone_config_path)
        qzone_cfg = QzoneRuntimeConfig(self.qzone_config_raw, self.qzone_config_path)
        qzone_cfg.client = await self._bind_qzone_client(self._find_qzone_client(event))
        refresh_error: Exception | None = None

        if self.config.auto_refresh_qzone_cookies and qzone_cfg.client is not None:
            try:
                await self._refresh_qzone_runtime_cookies(qzone_cfg)
            except Exception as exc:
                refresh_error = exc
                logger.warning(
                    "[QzoneSelfieBridge] qzone cookie refresh failed before publish: origin=%s error=%s",
                    origin or self.DEFAULT_ORIGIN,
                    exc,
                )
        else:
            await self._sync_live_qzone_cookies(qzone_cfg.cookies_str)

        try:
            await self._probe_qzone_ready(qzone_cfg)
            logger.info(
                "[QzoneSelfieBridge] qzone precheck passed: origin=%s",
                origin or self.DEFAULT_ORIGIN,
            )
        except Exception as probe_exc:
            error_text = str(probe_exc)
            if self._looks_like_qzone_login_error(error_text):
                try:
                    qzone_cfg.update_cookies("")
                    await self._sync_live_qzone_cookies("")
                except Exception as clear_exc:
                    logger.warning(
                        "[QzoneSelfieBridge] clear stale qzone cookies failed: %s",
                        clear_exc,
                    )
                if qzone_cfg.client is not None:
                    try:
                        await self._refresh_qzone_runtime_cookies(qzone_cfg)
                        await self._probe_qzone_ready(qzone_cfg)
                        logger.info(
                            "[QzoneSelfieBridge] qzone precheck recovered after clearing stale cookies: origin=%s",
                            origin or self.DEFAULT_ORIGIN,
                        )
                        return
                    except Exception as retry_exc:
                        error_text = f"{error_text}；清理后重试仍失败：{retry_exc}"

            detail = error_text
            if refresh_error is not None:
                detail = f"{detail}；自动刷新 cookies 失败：{refresh_error}"
            logger.warning(
                "[QzoneSelfieBridge] qzone precheck skipped hard failure because error is not login-related: origin=%s error=%s",
                origin or self.DEFAULT_ORIGIN,
                detail,
            )
            return

    async def _repair_qzone_login_state(
        self,
        *,
        event: AstrMessageEvent | None = None,
        origin: str | None = None,
    ) -> None:
        self.qzone_config_raw = self._read_json(self.qzone_config_path)
        qzone_cfg = QzoneRuntimeConfig(self.qzone_config_raw, self.qzone_config_path)
        qzone_cfg.client = await self._bind_qzone_client(self._find_qzone_client(event))
        if qzone_cfg.client is None:
            raise RuntimeError("当前没有可用 bot client，无法自动重新登录 QQ 空间")

        qzone_cfg.update_cookies("")
        await self._sync_live_qzone_cookies("")
        await self._refresh_qzone_runtime_cookies(qzone_cfg)
        await self._probe_qzone_ready(qzone_cfg)
        logger.info(
            "[QzoneSelfieBridge] qzone login repaired and revalidated: origin=%s",
            origin or self.DEFAULT_ORIGIN,
        )

    def _patch_qzone_publishers(self, target_plugin: Any | None = None):
        if not self.config.takeover_qzone_publish:
            return

        plugins = [target_plugin] if target_plugin is not None else list(
            self._iter_qzone_plugins()
        )
        for plugin in plugins:
            if plugin is None or plugin is self:
                continue
            service = getattr(plugin, "service", None)
            if service is None:
                continue
            if getattr(service, "_qzone_selfie_bridge_patched", False):
                continue

            original_publish_post = service.publish_post

            async def wrapped_publish_post(
                *,
                post: Post | None = None,
                text: str | None = None,
                images: list[Any] | None = None,
                _service: Any = service,
                _original: Callable[..., Awaitable[Post]] = original_publish_post,
            ) -> Post:
                if post is not None:
                    return await _original(post=post, text=text, images=images)
                published_post, _caption, _image_path = await self.publish_selfie_post(
                    extra=text,
                    origin=self.DEFAULT_ORIGIN,
                    original_images=images,
                    service=_service,
                )
                return published_post

            service.publish_post = wrapped_publish_post
            service._qzone_selfie_bridge_patched = True
            service._qzone_selfie_bridge_original_publish_post = original_publish_post
            self._patched_qzone_services[id(service)] = (service, original_publish_post)
            logger.info(
                "[QzoneSelfieBridge] 已接管 qzone 发帖流程: service=%s",
                service.__class__.__name__,
            )

    def _unpatch_qzone_publishers(self):
        for service, original in self._patched_qzone_services.values():
            try:
                service.publish_post = original
                service._qzone_selfie_bridge_patched = False
            except Exception as exc:
                logger.warning("[QzoneSelfieBridge] unpatch failed: %s", exc)
        self._patched_qzone_services.clear()

    async def _run_custom_publish_job(self, time_spec: str) -> None:
        logger.info(
            "[QzoneSelfieBridge] custom publish trigger fired: time=%s", time_spec
        )
        try:
            post, caption, image_path = await self.publish_selfie_post(
                origin=f"{self.DEFAULT_ORIGIN}:scheduled:{time_spec}",
            )
            logger.info(
                "[QzoneSelfieBridge] custom publish success: time=%s tid=%s caption=%s",
                time_spec,
                post.tid or "unknown",
                caption,
            )
            await self._notify_auto_publish_result(
                success=True,
                time_spec=time_spec,
                post=post,
                caption=caption,
                image_path=image_path,
            )
        except Exception as exc:
            logger.error(
                "[QzoneSelfieBridge] custom publish failed: time=%s error=%s",
                time_spec,
                exc,
                exc_info=True,
            )
            await self._notify_auto_publish_result(
                success=False,
                time_spec=time_spec,
                error=str(exc),
            )

    def _resolve_auto_notify_targets(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        user_ids = list(self.config.notify_target_users)
        group_ids = list(self.config.notify_target_groups)
        if user_ids or group_ids:
            return tuple(user_ids), tuple(group_ids)

        sender = self._find_qzone_sender()
        sender_cfg = getattr(sender, "cfg", None)
        if sender_cfg is None:
            return (), ()

        manage_group = str(getattr(sender_cfg, "manage_group", "") or "").strip()
        if manage_group.isdigit() and manage_group not in group_ids:
            group_ids.append(manage_group)

        for admin_id in getattr(sender_cfg, "admins_id", []) or []:
            admin_id = str(admin_id).strip()
            if admin_id.isdigit() and admin_id not in user_ids:
                user_ids.append(admin_id)

        return tuple(user_ids), tuple(group_ids)

    async def _build_notify_ob_message(
        self,
        *,
        message: str,
        image_path: Path | None = None,
    ) -> list[dict]:
        chain = [Plain(message)]
        if image_path is not None and image_path.exists():
            chain.append(CoreImage.fromFileSystem(str(image_path)))
        return await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

    async def _notify_auto_publish_result(
        self,
        *,
        success: bool,
        time_spec: str,
        post: Post | None = None,
        caption: str | None = None,
        image_path: Path | None = None,
        error: str | None = None,
    ) -> None:
        if success and not self.config.notify_on_success:
            return
        if not success and not self.config.notify_on_failure:
            return

        client = self._find_qzone_client()
        if client is None:
            logger.warning(
                "[QzoneSelfieBridge] skip auto publish notify because no client is available"
            )
            return

        user_ids, group_ids = self._resolve_auto_notify_targets()
        if not user_ids and not group_ids:
            return

        if success:
            message = (
                f"定时自拍说说成功 {time_spec}\n"
                f"TID：{post.tid if post is not None and post.tid else 'unknown'}\n"
                f"文案：{caption or ''}"
            )
        else:
            message = f"定时自拍说说失败 {time_spec}\n原因：{error or '未知错误'}"

        obmsg = await self._build_notify_ob_message(
            message=message,
            image_path=image_path if success else None,
        )

        for group_id in group_ids:
            try:
                await client.send_group_msg(group_id=int(group_id), message=obmsg)
            except Exception as exc:
                logger.warning(
                    "[QzoneSelfieBridge] notify group failed: group=%s error=%s",
                    group_id,
                    exc,
                )

        for user_id in user_ids:
            try:
                await client.send_private_msg(user_id=int(user_id), message=obmsg)
            except Exception as exc:
                logger.warning(
                    "[QzoneSelfieBridge] notify user failed: user=%s error=%s",
                    user_id,
                    exc,
                )

    async def _get_or_create_schedule(
        self,
        *,
        origin: str | None = None,
        extra: str | None = None,
    ) -> ScheduleData:
        today = dt.datetime.now()
        # Reload shared life-scheduler data on each publish so manual rewrites
        # from the sibling plugin are visible without restarting AstrBot.
        try:
            self.life_data_mgr.load()
        except Exception as exc:
            logger.warning(
                "[QzoneSelfieBridge] reload life schedule cache failed: %s", exc
            )
        data = self.life_data_mgr.get(today)
        if data and data.status == "ok":
            return data

        if not self.config.regenerate_life_when_missing:
            raise RuntimeError("当天生活日程不存在，且未开启自动补生成。")

        data = await self.life_generator.generate_schedule(
            today,
            origin or self.DEFAULT_ORIGIN,
            extra=extra,
        )
        if data.status != "ok":
            raise RuntimeError("生活日程生成失败。")
        return data

    def _build_selfie_prompt(
        self, schedule: ScheduleData, extra: str | None = None
    ) -> str:
        character_traits = self.config.selfie_character_traits.strip()
        character_traits_block = (
            f"\u989d\u5916\u89d2\u8272\u7279\u5f81\uff1a{character_traits}\u3002"
            if character_traits
            else ""
        )
        return self.config.selfie_prompt_template.format(
            outfit_style=schedule.outfit_style or "自然日常风",
            outfit=schedule.outfit or "日常穿搭",
            schedule=schedule.schedule or "今天按计划生活",
            character_traits=character_traits,
            character_traits_block=character_traits_block,
            extra=(extra or "").strip(),
        ).strip()

    def _selfie_feature_conf(self) -> dict[str, Any]:
        features = self.gitee_config_raw.get("features") or {}
        return features.get("selfie") or {}

    def _edit_feature_conf(self) -> dict[str, Any]:
        features = self.gitee_config_raw.get("features") or {}
        return features.get("edit") or {}

    def _resolve_data_rel_path(self, rel_path: str) -> Path | None:
        if not isinstance(rel_path, str) or not rel_path.strip():
            return None
        rel = rel_path.replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p]
        if any(p in {".", ".."} for p in parts):
            return None
        base = self.gitee_data_dir.resolve(strict=False)
        target = (base / "/".join(parts)).resolve(strict=False)
        try:
            target.relative_to(base)
        except ValueError:
            return None
        return target

    def _get_config_selfie_reference_paths(self) -> list[Path]:
        conf = self._selfie_feature_conf()
        ref_list = conf.get("reference_images", [])
        if not isinstance(ref_list, list):
            return []

        paths: list[Path] = []
        for rel_path in ref_list:
            p = self._resolve_data_rel_path(str(rel_path))
            if p and p.is_file():
                paths.append(p)
        return paths

    def _get_selfie_ref_store_key(self, event: AstrMessageEvent | None = None) -> str:
        self_id = ""
        if event is not None:
            try:
                if hasattr(event, "get_self_id"):
                    self_id = str(event.get_self_id() or "").strip()
            except Exception:
                self_id = ""
        return f"bot_selfie_{self_id}" if self_id else "bot_selfie"

    async def _get_selfie_reference_paths(
        self, event: AstrMessageEvent | None = None
    ) -> tuple[list[Path], str]:
        webui_paths = self._get_config_selfie_reference_paths()
        if webui_paths:
            return webui_paths, "webui"

        store_paths = await self.refs.get_paths(self._get_selfie_ref_store_key(event))
        if store_paths:
            return store_paths, "store"

        return [], "none"

    async def _read_paths_bytes(self, paths: list[Path]) -> list[bytes]:
        out: list[bytes] = []
        for p in paths:
            try:
                data = await asyncio.to_thread(p.read_bytes)
            except Exception:
                continue
            if data:
                out.append(data)
        return out

    async def _image_segs_to_bytes(self, image_segs: list[Any]) -> list[bytes]:
        out: list[bytes] = []
        for seg in image_segs:
            try:
                b64 = await seg.convert_to_base64()
                out.append(decode_base64_image_payload(b64))
            except Exception as exc:
                logger.warning("[QzoneSelfieBridge] convert image seg failed: %s", exc)
        return out

    async def _coerce_images_to_bytes(
        self, images: Iterable[Any] | None = None
    ) -> list[bytes]:
        out: list[bytes] = []
        for item in images or []:
            if isinstance(item, bytes):
                out.append(item)
                continue

            if isinstance(item, Path):
                try:
                    out.append(await asyncio.to_thread(item.read_bytes))
                except Exception as exc:
                    logger.warning(
                        "[QzoneSelfieBridge] read local extra image failed: %s",
                        exc,
                    )
                continue

            if isinstance(item, str):
                local_path = Path(item)
                if local_path.exists() and local_path.is_file():
                    try:
                        out.append(await asyncio.to_thread(local_path.read_bytes))
                    except Exception as exc:
                        logger.warning(
                            "[QzoneSelfieBridge] read path extra image failed: %s",
                            exc,
                        )
                    continue

                data = await download_remote_image(item)
                if data:
                    out.append(data)
                continue

        return out

    def _normalize_chain_item(self, item: object) -> dict[str, str] | None:
        if isinstance(item, str):
            pid = item.strip()
            return {"provider_id": pid} if pid else None
        if isinstance(item, dict):
            pid = str(item.get("provider_id") or "").strip()
            if pid:
                return {"provider_id": pid, "output": str(item.get("output") or "")}
        return None

    def _merge_selfie_chain_with_edit_chain(
        self, selfie_chain: list[object]
    ) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen: set[str] = set()

        def append_unique(items: list[object]):
            for item in items:
                normalized = self._normalize_chain_item(item)
                if not normalized:
                    continue
                pid = str(normalized.get("provider_id") or "").strip()
                if not pid or pid in seen:
                    continue
                merged.append(normalized)
                seen.add(pid)

        append_unique(selfie_chain)
        edit_chain = self._edit_feature_conf().get("chain", [])
        if isinstance(edit_chain, list):
            append_unique(edit_chain)
        return merged

    async def _generate_selfie_image(
        self,
        *,
        selfie_prompt: str,
        event: AstrMessageEvent | None = None,
        original_images: list[Any] | None = None,
    ) -> Path:
        conf = self._selfie_feature_conf()
        if not bool(conf.get("enabled", True)):
            raise RuntimeError("gitee_aiimg 的自拍功能未启用。")

        ref_paths, _ = await self._get_selfie_reference_paths(event)
        ref_images = await self._read_paths_bytes(ref_paths)
        if not ref_images:
            raise RuntimeError(
                "未找到自拍参考图，请先在 gitee_aiimg 中设置自拍参考。"
            )

        extra_bytes: list[bytes] = []
        if event is not None:
            extra_segs = await get_images_from_event(event, include_avatar=False)
            extra_bytes = await self._image_segs_to_bytes(extra_segs)
        elif original_images:
            extra_bytes = await self._coerce_images_to_bytes(original_images)

        images = [*ref_images, *extra_bytes]

        chain_override: list[dict[str, str]] | None = None
        raw_chain = conf.get("chain", [])
        use_edit_chain = bool(conf.get("use_edit_chain_when_empty", True))
        if isinstance(raw_chain, list) and raw_chain:
            normalized = [
                item
                for item in (self._normalize_chain_item(x) for x in raw_chain)
                if item is not None
            ]
            if normalized:
                chain_override = normalized
                if use_edit_chain:
                    chain_override = self._merge_selfie_chain_with_edit_chain(
                        chain_override
                    )

        task_types = conf.get("gitee_task_types")
        if isinstance(task_types, list) and task_types:
            final_task_types = [str(x).strip() for x in task_types if str(x).strip()]
        else:
            final_task_types = ["id", "background", "style"]

        default_output = str(conf.get("default_output") or "").strip() or None
        return await self.edit.edit(
            prompt=selfie_prompt,
            images=images,
            task_types=final_task_types,
            default_output=default_output,
            chain_override=chain_override,
        )

    def _get_provider(self, origin: str | None = None):
        try:
            return self.context.get_using_provider(origin)
        except Exception:
            return self.context.get_using_provider()

    def _get_prompt_optimizer_provider(self, origin: str | None = None):
        provider_id = self.config.selfie_prompt_optimizer_provider_id.strip()
        if provider_id:
            try:
                return self.context.get_provider_by_id(provider_id)
            except Exception as exc:
                logger.warning(
                    "[QzoneSelfieBridge] get prompt optimizer provider failed: %s",
                    exc,
                )
                return None
        return self._get_provider(origin)

    @staticmethod
    def _get_provider_debug_name(provider: Any) -> str:
        provider_cfg = getattr(provider, "provider_config", None)
        if isinstance(provider_cfg, dict):
            return str(
                provider_cfg.get("id")
                or provider_cfg.get("model")
                or provider_cfg.get("provider_source_id")
                or type(provider).__name__
            )
        return type(provider).__name__

    @staticmethod
    def _normalize_optimizer_prompt_text(text: str, fallback: str) -> str:
        raw = (text or fallback or "").strip()
        if not raw:
            return fallback

        raw = raw.replace("\r", "\n")
        for prefix in (
            "\u63d0\u793a\u8bcd\uff1a",
            "\u63d0\u793a\u8bcd:",
            "prompt:",
            "Prompt:",
            "\u4f18\u5316\u540e\u63d0\u793a\u8bcd\uff1a",
            "\u4f18\u5316\u540e\u63d0\u793a\u8bcd:",
        ):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()

        raw = (
            raw.replace("\u201c", "")
            .replace("\u201d", "")
            .replace('"', "")
            .replace("\u2018", "")
            .replace("\u2019", "")
        )
        lines = [line.strip(" -*\t") for line in raw.splitlines() if line.strip()]
        raw = " ".join(lines).strip()
        raw = re.sub(r"\s+", " ", raw)
        raw = raw[:500].strip()
        return raw or fallback

    async def _optimize_selfie_prompt(
        self,
        *,
        base_prompt: str,
        schedule: ScheduleData,
        extra: str | None = None,
        origin: str | None = None,
    ) -> str:
        if not self.config.optimize_selfie_prompt:
            return base_prompt

        configured_provider_id = (
            self.config.selfie_prompt_optimizer_provider_id.strip()
            or "(follow_current_session)"
        )
        provider = self._get_prompt_optimizer_provider(origin)
        if provider is None or not hasattr(provider, "text_chat"):
            logger.warning(
                "[QzoneSelfieBridge] selfie prompt optimizer skipped: configured_provider=%s provider_unavailable=true",
                configured_provider_id,
            )
            return base_prompt
        provider_name = self._get_provider_debug_name(provider)

        character_traits = self.config.selfie_character_traits.strip()
        character_traits_block = (
            f"\u989d\u5916\u89d2\u8272\u7279\u5f81\uff1a{character_traits}\u3002"
            if character_traits
            else ""
        )
        prompt = self.config.selfie_prompt_optimizer_template.format(
            base_prompt=base_prompt,
            outfit_style=schedule.outfit_style or "\u81ea\u7136\u65e5\u5e38\u98ce",
            outfit=schedule.outfit or "\u65e5\u5e38\u7a7f\u642d",
            schedule=schedule.schedule or "\u4eca\u5929\u6309\u8ba1\u5212\u751f\u6d3b",
            character_traits=character_traits,
            character_traits_block=character_traits_block,
            extra=(extra or "").strip(),
        )
        session_id = (
            f"qzone_selfie_prompt_optimizer_{int(dt.datetime.now().timestamp())}"
        )
        logger.info(
            "[QzoneSelfieBridge] selfie prompt optimizer start: configured_provider=%s actual_provider=%s base_len=%s optimizer_prompt_len=%s",
            configured_provider_id,
            provider_name,
            len(base_prompt),
            len(prompt),
        )
        try:
            resp = await provider.text_chat(prompt, session_id=session_id)
            text = self._extract_completion_text(resp)
            if text:
                optimized = self._normalize_optimizer_prompt_text(text, base_prompt)
                preview = optimized[:120].replace("\n", " ").strip()
                logger.info(
                    "[QzoneSelfieBridge] selfie prompt optimized: provider=%s base_len=%s optimized_len=%s changed=%s preview=%s",
                    provider_name,
                    len(base_prompt),
                    len(optimized),
                    optimized != base_prompt,
                    preview,
                )
                return optimized
            logger.warning(
                "[QzoneSelfieBridge] selfie prompt optimizer returned empty text: provider=%s",
                provider_name,
            )
        except Exception as exc:
            logger.warning(
                "[QzoneSelfieBridge] selfie prompt optimizer failed: provider=%s error=%s",
                provider_name,
                exc,
            )
        finally:
            await self._cleanup_temp_session(session_id)
        return base_prompt

    async def _generate_caption(
        self,
        *,
        schedule: ScheduleData,
        selfie_prompt: str,
        extra: str | None = None,
        origin: str | None = None,
    ) -> str:
        outfit_style = schedule.outfit_style or "\u81ea\u7136\u65e5\u5e38\u98ce"
        outfit = schedule.outfit or "\u65e5\u5e38\u7a7f\u642d"
        day_schedule = schedule.schedule or "\u4eca\u5929\u6309\u8ba1\u5212\u751f\u6d3b"
        prompt = self.config.caption_prompt_template.format(
            outfit_style=outfit_style,
            outfit=outfit,
            schedule=day_schedule,
            selfie_prompt=selfie_prompt,
            extra=(extra or "").strip(),
        )
        fallback = self.config.fallback_caption_template.format(
            outfit_style=outfit_style,
            outfit=outfit,
            schedule=day_schedule,
        ).strip()

        provider = self._get_provider(origin)
        if provider:
            session_id = f"qzone_selfie_caption_{int(dt.datetime.now().timestamp())}"
            try:
                resp = await provider.text_chat(prompt, session_id=session_id)
                text = self._extract_completion_text(resp)
                if text:
                    return self._normalize_caption_text(text, fallback)
            except Exception as exc:
                logger.warning("[QzoneSelfieBridge] caption llm failed: %s", exc)
            finally:
                await self._cleanup_temp_session(session_id)

        return self._normalize_caption_text("", fallback)

    @staticmethod
    def _normalize_caption_text(text: str, fallback: str) -> str:
        raw = (text or fallback or "").strip()
        if not raw:
            return "\u6211\u4eca\u5929\u968f\u624b\u62cd\u4e86\u4e00\u5f20\u3002"

        raw = raw.replace("\r", "\n")
        for prefix in (
            "\u6587\u6848\uff1a",
            "\u6587\u6848:",
            "\u8bf4\u8bf4\uff1a",
            "\u8bf4\u8bf4:",
            "\u7a7a\u95f4\u6587\u6848\uff1a",
            "\u7a7a\u95f4\u6587\u6848:",
            "\u914d\u6587\uff1a",
            "\u914d\u6587:",
        ):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()

        raw = (
            raw.replace("\u201c", "")
            .replace("\u201d", "")
            .replace('"', "")
            .replace("\u2018", "")
            .replace("\u2019", "")
        )
        lines = [line.strip(" -*\t") for line in raw.splitlines() if line.strip()]
        raw = "".join(lines).strip()
        raw = re.split(r"[\u3002\uff01\uff1f!?\uff1b;\n]+", raw, maxsplit=1)[0].strip()
        raw = re.sub(r"\s+", "", raw)

        if not raw:
            raw = fallback.strip()

        raw = raw[:36].strip("\uff0c,\u3001\u3002\uff01\uff1f!?\uff1b;\uff1a: ")
        if not raw:
            raw = "\u6211\u4eca\u5929\u968f\u624b\u62cd\u4e86\u4e00\u5f20"

        if "\u6211" not in raw and not raw.startswith(
            (
                "\u4eca\u5929",
                "\u521a",
                "\u8fd9\u8eab",
                "\u8fd9\u5957",
                "\u51fa\u95e8",
                "\u4e0b\u73ed",
                "\u8def\u4e0a",
                "\u665a\u4e0a",
                "\u65e9\u4e0a",
                "\u5348\u540e",
            )
        ):
            raw = f"\u6211{raw}"

        raw = raw[:36].strip("\uff0c,\u3001\u3002\uff01\uff1f!?\uff1b;\uff1a: ")
        if not raw:
            raw = "\u6211\u4eca\u5929\u968f\u624b\u62cd\u4e86\u4e00\u5f20"
        return f"{raw}\u3002"

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
        if resp is None:
            return ""
        for key in ("completion_text", "completion", "text", "content"):
            value = getattr(resp, key, None)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        return ""

    async def _cleanup_temp_session(self, sid: str):
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(sid)
            if cid:
                await self.context.conversation_manager.delete_conversation(sid, cid)
        except Exception:
            pass

    async def _build_selfie_publish_bundle(
        self,
        *,
        extra: str | None = None,
        event: AstrMessageEvent | None = None,
        origin: str | None = None,
        original_images: list[Any] | None = None,
    ) -> tuple[str, list[Any], list[str], Path]:
        schedule = await self._get_or_create_schedule(origin=origin, extra=extra)
        selfie_prompt = self._build_selfie_prompt(schedule, extra)
        optimized_selfie_prompt = await self._optimize_selfie_prompt(
            base_prompt=selfie_prompt,
            schedule=schedule,
            extra=extra,
            origin=origin,
        )
        image_path = await self._generate_selfie_image(
            selfie_prompt=optimized_selfie_prompt,
            event=event,
            original_images=original_images,
        )
        caption = await self._generate_caption(
            schedule=schedule,
            selfie_prompt=selfie_prompt,
            extra=extra,
            origin=origin,
        )

        selfie_bytes = await asyncio.to_thread(image_path.read_bytes)
        publish_images: list[Any] = []
        preview_images: list[str] = []

        if self.config.append_selfie_to_existing_images and original_images:
            publish_images.extend(original_images)
            for item in original_images:
                if isinstance(item, str):
                    preview_images.append(item)
                elif isinstance(item, Path):
                    preview_images.append(str(item))

        publish_images.append(selfie_bytes)
        preview_images.append(str(image_path))

        return caption, publish_images, preview_images, image_path

    async def _publish_via_service(
        self,
        service: Any,
        *,
        event: AstrMessageEvent | None = None,
        origin: str | None = None,
        caption: str,
        publish_images: list[Any],
        preview_images: list[str],
    ) -> Post:
        temp_post = SimpleNamespace(text=caption, images=publish_images)

        async def do_publish() -> Any:
            resp = await service.qzone.publish(temp_post)
            if not resp.ok:
                detail = resp.message or resp.data or resp.code
                raise RuntimeError(f"QQ空间发布失败：{detail}")
            return resp

        try:
            resp = await do_publish()
        except Exception as exc:
            if not self._looks_like_qzone_login_error(str(exc)):
                raise
            logger.warning(
                "[QzoneSelfieBridge] qzone publish via service hit login error, retrying once: origin=%s error=%s",
                origin or self.DEFAULT_ORIGIN,
                exc,
            )
            await self._repair_qzone_login_state(event=event, origin=origin)
            resp = await do_publish()

        uin = await service.session.get_uin()
        name = await service.session.get_nickname()
        post = Post(
            uin=uin,
            name=name,
            text=caption,
            images=preview_images,
        )
        post.tid = str(resp.data.get("tid") or "")
        post.status = "approved"
        post.create_time = int(resp.data.get("now") or post.create_time)
        await service.db.save(post)
        return post

    async def _publish_direct_to_qzone(
        self,
        *,
        event: AstrMessageEvent | None,
        origin: str | None = None,
        caption: str,
        publish_images: list[Any],
        preview_images: list[str],
    ) -> Post:
        async def run_once() -> Post:
            qzone_cfg = QzoneRuntimeConfig(self.qzone_config_raw, self.qzone_config_path)
            qzone_cfg.client = self._find_qzone_client(event)
            session = QzoneSession(qzone_cfg)
            api = QzoneAPI(session, qzone_cfg)
            try:
                temp_post = SimpleNamespace(text=caption, images=publish_images)
                resp = await api.publish(temp_post)
                if not resp.ok:
                    detail = resp.message or resp.data or resp.code
                    raise RuntimeError(f"QQ空间发布失败：{detail}")

                uin = await session.get_uin()
                name = await session.get_nickname()
                post = Post(
                    uin=uin,
                    name=name,
                    text=caption,
                    images=preview_images,
                )
                post.tid = str(resp.data.get("tid") or "")
                post.status = "approved"
                post.create_time = int(resp.data.get("now") or post.create_time)
                return post
            finally:
                await api.close()

        try:
            return await run_once()
        except Exception as exc:
            if not self._looks_like_qzone_login_error(str(exc)):
                raise
            logger.warning(
                "[QzoneSelfieBridge] qzone direct publish hit login error, retrying once: origin=%s error=%s",
                origin or self.DEFAULT_ORIGIN,
                exc,
            )
            await self._repair_qzone_login_state(event=event, origin=origin)
            return await run_once()
