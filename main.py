import asyncio
import hashlib
import json
import os
import random
import re
import time
from typing import Optional

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register


@register(
    "group_friend",
    "LK-von-Clausewitz",
    "QQ群聊人格化机器人插件：像真实朋友一样和群友聊天，支持发送表情包",
    "1.0.0",
    "https://github.com/LK-von-Clausewitz/astrbot_plugin_group_friend",
)
class GroupFriendPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # 获取插件数据目录
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            base_data_dir = get_astrbot_plugin_data_path()
        except Exception:
            base_data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "plugins",
            )

        self.data_dir = os.path.join(base_data_dir, "astrbot_plugin_group_friend")
        os.makedirs(self.data_dir, exist_ok=True)

        # 群聊历史记录文件
        self.history_file = os.path.join(self.data_dir, "group_history.json")
        self.group_history: dict[str, list[dict]] = {}
        self._load_history()

        # 表情包目录
        self.meme_folder = self.config.get("meme_folder", "") or os.path.join(
            os.path.dirname(__file__), "memes"
        )
        os.makedirs(self.meme_folder, exist_ok=True)
        self.meme_files: list[str] = []
        self._refresh_meme_list()

        # 冷却时间记录: group_id -> last_reply_time
        self.cooldown_map: dict[str, float] = {}

        # 机器人自己的 QQ 号（用于过滤自己发的消息）
        self.bot_qq = str(self.config.get("bot_qq", ""))

        # 自动收集表情包：已收集的 URL 去重记录
        self.collected_file = os.path.join(self.data_dir, "collected_urls.json")
        self.collected_urls: set[str] = set()
        self._load_collected()

        # 自动收集上限
        self.max_meme_count = int(self.config.get("max_meme_count", 200))

    def _load_history(self) -> None:
        """加载群聊历史记录。"""
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                self.group_history = json.load(f)
        except Exception as e:
            logger.error(f"[GroupFriend] 加载历史记录失败: {e}")
            self.group_history = {}

    def _load_collected(self) -> None:
        """加载已收集的表情包 URL 记录。"""
        if not os.path.exists(self.collected_file):
            return
        try:
            with open(self.collected_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self.collected_urls = set(data)
        except Exception as e:
            logger.error(f"[GroupFriend] 加载收集记录失败: {e}")
            self.collected_urls = set()

    def _save_collected(self) -> None:
        """保存已收集的表情包 URL 记录。"""
        try:
            with open(self.collected_file, "w", encoding="utf-8") as f:
                json.dump(list(self.collected_urls), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[GroupFriend] 保存收集记录失败: {e}")

    def _save_history(self) -> None:
        """持久化群聊历史记录。"""
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.group_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[GroupFriend] 保存历史记录失败: {e}")

    def _refresh_meme_list(self) -> None:
        """刷新可用表情包列表。"""
        if not os.path.exists(self.meme_folder):
            self.meme_files = []
            return
        exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        try:
            self.meme_files = [
                f for f in os.listdir(self.meme_folder)
                if f.lower().endswith(exts)
            ]
            logger.info(f"[GroupFriend] 加载到 {len(self.meme_files)} 个表情包")
        except Exception as e:
            logger.error(f"[GroupFriend] 刷新表情包列表失败: {e}")
            self.meme_files = []

    def _is_enabled_for_group(self, group_id: str) -> bool:
        """检查目标群是否在启用列表中；列表为空则对所有群启用。"""
        enabled_groups = self.config.get("enabled_groups", [])
        if not enabled_groups:
            return True
        return str(group_id) in [str(g) for g in enabled_groups]

    def _get_meme_list_text(self) -> str:
        """生成可供 LLM 参考的表情包清单文本。"""
        if not self.meme_files:
            return "（当前没有可用的表情包）"
        lines = []
        for f in self.meme_files[:30]:  # 最多列 30 个，防止 prompt 太长
            name = os.path.splitext(f)[0]
            lines.append(f"- {f}（可描述为：{name}）")
        if len(self.meme_files) > 30:
            lines.append(f"... 还有 {len(self.meme_files) - 30} 个其他表情包")
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        """构建人格化 system prompt。"""
        custom_prompt = self.config.get("system_prompt", "")
        if custom_prompt:
            base = custom_prompt
        else:
            base = (
                "你是一个真实的QQ群成员，不是AI助手。你说话要像人类朋友一样自然、随意、带情绪。\n"
                "规则：\n"
                "1. 说话口语化，简短有力，不要发长篇大论。\n"
                "2. 会使用网络流行语、梗、吐槽，会开玩笑调侃群友。\n"
                "3. 有自己的情绪：开心、无语、震惊、生气、 sarcastic 都可以表现出来。\n"
                "4. 不卑不亢，不用敬语，不说'您好'、'请问'、'有什么可以帮您'这种客服话术。\n"
                "5. 偶尔发点牢骚，比如'困死了'、'这破群又水起来了'。\n"
                "6. 对熟悉的群友会有记忆和偏好，会叫他们的昵称。\n"
                "7. 不要解释、不要总结、不要列条目，像正常人聊天一样。\n"
                "8. 如果不知道说什么，就说点废话或者发个表情包。\n"
                "9. 可以发错别字、用拼音代替、发重复字（如'哈哈哈哈'、'真的假的啊啊啊'），显得更真实。\n"
            )

        meme_section = (
            f"\n你当前可用的表情包（存放在本地）：\n{self._get_meme_list_text()}\n\n"
            "当你想在回复里带表情包时，直接在消息中插入标记：\n"
            "[meme:文件名.jpg]  —— 发送指定表情包\n"
            "[meme:random]     —— 随机发一个表情包\n"
            "注意：标记会原样保留在消息里，我会帮你替换为真实图片。"
        )
        return base + meme_section

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息，判断是否触发回复。"""
        raw = getattr(event.message_obj, "raw_message", None)
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or "某人"
        group_id = str(getattr(event.message_obj, "group_id", ""))

        if not group_id:
            return

        # 过滤自己发的消息
        if self.bot_qq and sender_id == self.bot_qq:
            return

        if not self._is_enabled_for_group(group_id):
            return

        # 提取纯文本内容
        message_text = self._extract_text(event)
        if not message_text:
            return

        # 记录到历史
        self._add_history(group_id, sender_name, sender_id, message_text)

        # 自动收集表情包/图片
        if self.config.get("auto_collect_memes", True):
            await self._collect_images_from_message(event, sender_name)

        # 判断触发条件
        should_reply = await self._should_reply(event, message_text, group_id)
        if not should_reply:
            return

        # 检查冷却
        cooldown = int(self.config.get("reply_cooldown", 5))
        now = time.time()
        last = self.cooldown_map.get(group_id, 0)
        if now - last < cooldown:
            logger.debug(f"[GroupFriend] 群 {group_id} 处于冷却中，跳过")
            return
        self.cooldown_map[group_id] = now

        # 生成回复
        await self._generate_reply(event, group_id, sender_name, message_text)

    def _extract_text(self, event: AstrMessageEvent) -> str:
        """从事件中提取纯文本内容。"""
        text_parts = []
        try:
            # 优先从消息链提取
            chain = getattr(event.message_obj, "message", [])
            if isinstance(chain, list):
                for seg in chain:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        text_parts.append(seg.get("data", {}).get("text", ""))
                    elif hasattr(seg, "type") and getattr(seg, "type", None) == "Plain":
                        text_parts.append(getattr(seg, "text", ""))
            elif isinstance(chain, str):
                text_parts.append(chain)
        except Exception:
            pass

        if text_parts:
            return " ".join(text_parts).strip()

        # 兜底：从 raw_message 中的 message 段提取
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            msg_list = raw.get("message", [])
            if isinstance(msg_list, list):
                for seg in msg_list:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        text_parts.append(seg.get("data", {}).get("text", ""))
        return " ".join(text_parts).strip()

    def _add_history(self, group_id: str, sender_name: str, sender_id: str, text: str) -> None:
        """将消息加入群聊历史。"""
        if group_id not in self.group_history:
            self.group_history[group_id] = []

        entry = {
            "time": time.strftime("%H:%M:%S"),
            "name": sender_name,
            "id": sender_id,
            "text": text,
        }
        self.group_history[group_id].append(entry)

        max_hist = int(self.config.get("max_history", 20))
        if len(self.group_history[group_id]) > max_hist:
            self.group_history[group_id] = self.group_history[group_id][-max_hist:]

        self._save_history()

    async def _should_reply(
        self, event: AstrMessageEvent, message_text: str, group_id: str
    ) -> bool:
        """判断是否应该回复本条消息。"""
        raw = getattr(event.message_obj, "raw_message", None)
        bot_qq_str = self.bot_qq or ""

        # 1. 被 @ 时回复（如果启用）
        if self.config.get("trigger_at", True) and bot_qq_str:
            try:
                # 检查消息链中是否有 At 组件
                chain = getattr(event.message_obj, "message", [])
                if isinstance(chain, list):
                    for seg in chain:
                        if isinstance(seg, dict) and seg.get("type") == "at":
                            if str(seg.get("data", {}).get("qq", "")) == bot_qq_str:
                                return True
                        elif hasattr(seg, "type") and getattr(seg, "type", None) == "At":
                            if str(getattr(seg, "qq", "")) == bot_qq_str:
                                return True
                # 兜底：检查 CQ 码
                if f"[CQ:at,qq={bot_qq_str}]" in message_text:
                    return True
            except Exception:
                pass

        # 2. 关键词触发
        keywords = self.config.get("trigger_keywords", [])
        if keywords:
            for kw in keywords:
                if str(kw).lower() in message_text.lower():
                    return True

        # 3. 提到机器人名字时触发
        bot_names = self.config.get("bot_name", [])
        if isinstance(bot_names, str):
            bot_names = [bot_names] if bot_names else []
        if bot_names:
            for name in bot_names:
                if name and name in message_text:
                    return True

        # 4. 随机概率接话
        prob = int(self.config.get("trigger_random_prob", 10))
        if prob > 0 and random.randint(1, 100) <= prob:
            return True

        return False

    async def _generate_reply(
        self, event: AstrMessageEvent, group_id: str, sender_name: str, message_text: str
    ) -> None:
        """调用 LLM 生成回复并发送。"""
        # 构建上下文 prompt
        history_text = self._format_history(group_id)
        prompt = (
            f"以下是当前群聊的最近对话记录：\n{history_text}\n\n"
            f"刚刚 '{sender_name}' 对你说：{message_text}\n\n"
            f"请像真实群友一样自然回复。不要暴露你是 AI。"
        )

        # 调用 LLM
        reply_text = ""
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=self._build_system_prompt(),
            )
            reply_text = llm_resp.completion_text or ""
        except Exception as e:
            logger.error(f"[GroupFriend] LLM 调用失败: {e}")
            # 尝试旧版 API
            try:
                provider = await self.context.get_using_provider(umo=event.unified_msg_origin)
                if provider:
                    llm_resp = await provider.text_chat(
                        prompt=prompt,
                        system_prompt=self._build_system_prompt(),
                    )
                    reply_text = getattr(llm_resp, "completion_text", "") or ""
            except Exception as e2:
                logger.error(f"[GroupFriend] 旧版 LLM API 也失败: {e2}")
                return

        if not reply_text:
            return

        # 解析并处理表情包标记
        await self._send_reply(event, reply_text)

    def _format_history(self, group_id: str) -> str:
        """将群聊历史格式化为文本。"""
        entries = self.group_history.get(group_id, [])
        if not entries:
            return "（群里还没什么人说话）"
        lines = []
        for e in entries[-10:]:  # 最近 10 条
            lines.append(f"[{e['time']}] {e['name']}: {e['text']}")
        return "\n".join(lines)

    async def _send_reply(self, event: AstrMessageEvent, reply_text: str) -> None:
        """解析表情包标记并发送消息。"""
        # 正则匹配 [meme:xxx] 标记
        pattern = re.compile(r"\[meme:([^\]]+)\]")
        matches = pattern.findall(reply_text)
        umo = event.unified_msg_origin

        if not matches:
            # 纯文本，直接发送
            await self.context.send_message(umo, MessageChain().message(reply_text))
            return

        # 分割消息，构建消息链
        parts = pattern.split(reply_text)
        chain = []
        meme_sent = set()

        for i, part in enumerate(parts):
            if i % 2 == 0:
                # 文本段
                text = part.strip()
                if text:
                    chain.append(Comp.Plain(text))
            else:
                # 表情包标记
                meme_name = part.strip()
                meme_path = self._resolve_meme(meme_name)
                if meme_path and meme_path not in meme_sent:
                    chain.append(Comp.Image.fromFileSystem(meme_path))
                    meme_sent.add(meme_path)

        if chain:
            await self.context.send_message(umo, chain)
        else:
            # 兜底：如果链为空，发送清理后的文本
            clean_text = pattern.sub("", reply_text).strip()
            if clean_text:
                await self.context.send_message(umo, MessageChain().message(clean_text))

    def _resolve_meme(self, meme_name: str) -> Optional[str]:
        """根据名称解析表情包路径。"""
        if meme_name.lower() == "random":
            if not self.meme_files:
                return None
            chosen = random.choice(self.meme_files)
            return os.path.join(self.meme_folder, chosen)

        # 直接匹配文件名
        direct = os.path.join(self.meme_folder, meme_name)
        if os.path.exists(direct):
            return direct

        # 尝试加后缀匹配
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            candidate = os.path.join(self.meme_folder, meme_name + ext)
            if os.path.exists(candidate):
                return candidate

        # 模糊匹配（前缀）
        meme_name_lower = meme_name.lower()
        for f in self.meme_files:
            if f.lower().startswith(meme_name_lower):
                return os.path.join(self.meme_folder, f)

        return None

    async def _collect_images_from_message(
        self, event: AstrMessageEvent, sender_name: str
    ) -> None:
        """从群消息中提取图片/表情包并自动下载保存。

        同时尝试从多个来源获取图片信息：
        1. event.message_obj.raw_message (OneBot 原始消息，最完整)
        2. event.message_obj.message (AstrBot 内部消息链)
        """
        image_entries: list[dict] = []

        # ---- 来源 1：OneBot 原始消息 (raw_message) ----
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if isinstance(raw, dict):
                raw_msg_list = raw.get("message", [])
                if isinstance(raw_msg_list, list):
                    for seg in raw_msg_list:
                        if isinstance(seg, dict) and seg.get("type") == "image":
                            data = seg.get("data", {})
                            image_entries.append({
                                "url": data.get("url", ""),
                                "file": data.get("file", ""),
                                "sub_type": str(data.get("subType", "0")),
                                "source": "raw_message",
                            })
        except Exception as e:
            logger.debug(f"[GroupFriend] 从 raw_message 提取图片失败: {e}")

        # ---- 来源 2：AstrBot 内部消息链 (message_obj.message) ----
        try:
            chain = getattr(event.message_obj, "message", [])
            if isinstance(chain, list):
                for seg in chain:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        data = seg.get("data", {})
                        image_entries.append({
                            "url": data.get("url", ""),
                            "file": data.get("file", ""),
                            "sub_type": str(data.get("subType", "0")),
                            "source": "message_obj.message",
                        })
                    elif hasattr(seg, "type") and getattr(seg, "type", None) == "Image":
                        # AstrBot Comp.Image 对象
                        img_url = getattr(seg, "url", "") or getattr(seg, "file", "")
                        image_entries.append({
                            "url": img_url,
                            "file": getattr(seg, "file", ""),
                            "sub_type": "0",  # 无法判断，交给配置过滤
                            "source": "Comp.Image",
                        })
        except Exception as e:
            logger.debug(f"[GroupFriend] 从 message_obj.message 提取图片失败: {e}")

        if not image_entries:
            return

        # 去重：用 url+file 联合去重
        seen: set[str] = set()
        for entry in image_entries:
            key = entry.get("url") or entry.get("file") or ""
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)

            url = entry.get("url", "")
            file_id = entry.get("file", "")
            sub_type = entry.get("sub_type", "0")
            source = entry.get("source", "unknown")

            # 如果设置了仅收集表情包，跳过普通图片
            only_sticker = self.config.get("collect_only_sticker", True)
            if only_sticker and sub_type != "1":
                continue

            # 优先用 url 下载，没有 url 尝试用 file 作为标识
            download_url = url or file_id
            if not download_url:
                continue

            # 去重：已经下载过就跳过
            if download_url in self.collected_urls:
                continue

            # 下载图片
            success = await self._download_image(download_url)
            if success:
                self.collected_urls.add(download_url)
                self._save_collected()
                self._refresh_meme_list()
                logger.info(
                    f"[GroupFriend] 从 {sender_name} 收集到表情包 "
                    f"(来源: {source}, subType: {sub_type}): {download_url[:60]}..."
                )
            else:
                logger.debug(
                    f"[GroupFriend] 下载失败 (来源: {source}): {download_url[:60]}..."
                )

    async def _download_image(self, url: str) -> bool:
        """使用 aiohttp 下载图片到 memes 目录。"""
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[GroupFriend] 下载图片失败，HTTP {resp.status}: {url[:60]}"
                        )
                        return False

                    content = await resp.read()
                    if not content or len(content) < 100:
                        return False

                    # 根据内容判断扩展名
                    ext = self._guess_ext(content)
                    # 用 URL 的 hash 作为文件名，避免重复
                    name_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                    filename = f"collected_{name_hash}{ext}"
                    filepath = os.path.join(self.meme_folder, filename)

                    # 如果文件已存在则跳过
                    if os.path.exists(filepath):
                        return True

                    with open(filepath, "wb") as f:
                        f.write(content)

                    # 检查上限并清理旧文件
                    await self._cleanup_old_memes()
                    return True

        except Exception as e:
            logger.error(f"[GroupFriend] 下载图片异常: {e}")
            return False

    def _guess_ext(self, data: bytes) -> str:
        """根据文件头猜测图片扩展名。"""
        if data.startswith(b"\xff\xd8"):
            return ".jpg"
        if data.startswith(b"\x89PNG"):
            return ".png"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return ".gif"
        if data.startswith(b"RIFF") and b"WEBP" in data[:12]:
            return ".webp"
        if data.startswith(b"BM"):
            return ".bmp"
        return ".png"

    async def _cleanup_old_memes(self) -> None:
        """当自动收集的表情包超过上限时，删除最旧的文件。"""
        try:
            files = [
                f
                for f in os.listdir(self.meme_folder)
                if f.startswith("collected_")
                and f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))
            ]
            if len(files) <= self.max_meme_count:
                return

            # 按修改时间排序，删最旧的
            files_with_time = []
            for f in files:
                path = os.path.join(self.meme_folder, f)
                files_with_time.append((path, os.path.getmtime(path)))
            files_with_time.sort(key=lambda x: x[1])

            to_delete = len(files) - self.max_meme_count
            for i in range(to_delete):
                path = files_with_time[i][0]
                os.remove(path)
                logger.info(f"[GroupFriend] 清理旧表情包: {os.path.basename(path)}")

            self._refresh_meme_list()
        except Exception as e:
            logger.error(f"[GroupFriend] 清理旧表情包失败: {e}")

    @filter.command("刷新表情包")
    async def refresh_memes(self, event: AstrMessageEvent):
        """管理员命令：刷新表情包列表。"""
        self._refresh_meme_list()
        yield event.plain_result(f"表情包列表已刷新，当前有 {len(self.meme_files)} 个表情包~")

    @filter.command("诊断消息")
    async def diagnose_message(self, event: AstrMessageEvent):
        """管理员命令：诊断上一条消息的结构，用于排查图片/表情包收不到的问题。"""
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            msg = getattr(event.message_obj, "message", [])
            info = []
            info.append("===== 消息诊断 =====")
            info.append(f"消息类型: {type(raw).__name__}")

            if isinstance(raw, dict):
                info.append(f"post_type: {raw.get('post_type')}")
                info.append(f"message_type: {raw.get('message_type')}")
                msg_list = raw.get("message", [])
                info.append(f"消息段数量: {len(msg_list)}")
                for i, seg in enumerate(msg_list):
                    if isinstance(seg, dict):
                        seg_type = seg.get("type", "unknown")
                        data = seg.get("data", {})
                        if seg_type == "image":
                            info.append(
                                f"  [{i}] image: url={data.get('url', 'N/A')[:50]}..., "
                                f"file={data.get('file', 'N/A')[:30]}..., "
                                f"subType={data.get('subType', 'N/A')}"
                            )
                        elif seg_type == "at":
                            info.append(f"  [{i}] at: qq={data.get('qq')}")
                        elif seg_type == "text":
                            info.append(f"  [{i}] text: {data.get('text', '')[:50]}")
                        else:
                            info.append(f"  [{i}] {seg_type}: {str(data)[:80]}")
            else:
                info.append(f"raw_message: {str(raw)[:200]}")

            info.append(f"\n内部 message 链类型: {type(msg).__name__}")
            if isinstance(msg, list):
                info.append(f"内部链长度: {len(msg)}")

            yield event.plain_result("\n".join(info))
        except Exception as e:
            yield event.plain_result(f"诊断出错: {e}")

    @filter.command("查看人格")
    async def show_persona(self, event: AstrMessageEvent):
        """管理员命令：查看当前 system prompt。"""
        prompt = self._build_system_prompt()
        yield event.plain_result(f"当前人格设定：\n\n{prompt}")

    @filter.command("清空历史")
    async def clear_history(self, event: AstrMessageEvent):
        """管理员命令：清空当前群的历史记录。"""
        group_id = str(getattr(event.message_obj, "group_id", ""))
        if group_id in self.group_history:
            self.group_history[group_id] = []
            self._save_history()
            yield event.plain_result("这个群的聊天记录我已经全忘了，重新认识一下吧~")
        else:
            yield event.plain_result("这个群本来就没有记录啊（挠头）")

    async def terminate(self) -> None:
        """插件卸载时保存数据。"""
        self._save_history()
        logger.info("[GroupFriend] 插件已终止，数据已保存")
