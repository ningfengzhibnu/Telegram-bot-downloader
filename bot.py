"""
TG Bot Downloader — 自动下载 Telegram 已保存消息中的视频/文档

功能：
  - 监听「已保存消息」中的视频和文档，自动下载
  - 支持 t.me 链接下载（从频道/群组下载禁止转发的视频）
  - 断点续传（.part 文件缓存，中断后从断点继续）
  - 下载进度显示
  - 查重去重（同一视频不重复下载）
  - 启动时自动扫描未完成下载并续传
  - QR 码登录（无需手机号验证码）
  - 文件引用过期自动刷新
  - SOCKS5 代理支持
"""
import os
import asyncio
import sys
import re
import json
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events

import qrcode

# ====== 配置（通过环境变量注入） ======
API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
DOWNLOAD_PATH = os.environ.get("TG_DOWNLOAD_PATH", "/downloads")
SESSION_PATH = os.environ.get("TG_SESSION_PATH", "/app/session")
MAP_PATH = os.path.join(DOWNLOAD_PATH, "download_map.json")
IDS_PATH = os.path.join(DOWNLOAD_PATH, "downloaded_ids.json")

PROXY_HOST = os.environ.get("TG_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("TG_PROXY_PORT", "7890"))
PROXY = ("socks5", PROXY_HOST, PROXY_PORT) if PROXY_HOST else None

client = TelegramClient(SESSION_PATH, API_ID, API_HASH, proxy=PROXY)


# ====== 持久化辅助 ======


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def load_map():
    """下载映射：记录 t.me 链接来源，用于断点续传时直接定位"""
    return load_json(MAP_PATH)


def save_map(m):
    save_json(MAP_PATH, m)


def load_ids():
    """已下载 ID 集合：防止重复下载同一视频"""
    try:
        return set(load_json(IDS_PATH))
    except Exception:
        return set()


def save_ids(s):
    save_json(IDS_PATH, list(s))


# ====== 文件命名与解析 ======


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.strip('. ')
    return name[:200] if len(name) > 200 else name


def get_file_id(msg):
    """获取视频/文件的唯一 ID（同一文件转发多次 ID 不变）"""
    if msg.video:
        return msg.video.id
    if msg.document:
        return msg.document.id
    if msg.media:
        try:
            media = msg.media
            if hasattr(media, 'document') and media.document:
                return media.document.id
        except Exception:
            pass
    return None


def get_file_name(msg):
    """从消息中提取文件名：文字说明 > 原始文件名 > ID"""
    caption = None
    if msg.message and msg.message.strip():
        text = msg.message.strip().split('\n')[0]
        caption = sanitize_filename(text)

    ext = '.mp4'
    if msg.document:
        for attr in msg.document.attributes:
            name = getattr(attr, 'file_name', None)
            if name and '.' in name:
                ext = '.' + name.rsplit('.', 1)[-1].lower()
                break
    if ext not in ('.mp4', '.mkv', '.avi', '.webm', '.mov', '.flv', '.ts', '.m4v', '.wmv'):
        ext = '.mp4'

    if caption:
        return f"{caption}{ext}"
    if msg.document:
        for attr in msg.document.attributes:
            name = getattr(attr, 'file_name', None)
            if name:
                return name
        return f"{msg.document.id}{ext}"
    if msg.video:
        return f"{msg.video.id}{ext}"
    return None


def parse_tg_link(text):
    """解析 Telegram 消息链接，返回 (chat, msg_id) 或 None"""
    if not text:
        return None
    text = text.strip().strip('@')
    m = re.match(r'(?:https?://)?t\.me/(c/)?(\d+|[\w_]+)/(\d+)', text)
    if not m:
        return None
    is_private = bool(m.group(1))
    identifier = m.group(2)
    msg_id = int(m.group(3))
    if is_private:
        return (int(f"-100{identifier}"), msg_id)
    else:
        return (identifier, msg_id)


# ====== 下载核心（带断点续传） ======


async def download_with_progress(msg, save_path, file_name):
    """带进度条下载，支持断点续传。已完整跳过，残片从断点继续，引用过期自动刷新"""
    expected_size = None
    if msg.video:
        expected_size = msg.video.size
    elif msg.document:
        expected_size = msg.document.size
    elif msg.media:
        try:
            expected_size = msg.media.document.size
        except Exception:
            pass

    # 已完整 → 跳过
    if os.path.exists(save_path):
        local_size = os.path.getsize(save_path)
        if expected_size and local_size == expected_size:
            print(f"⏭  跳过（已完整）: {file_name} ({local_size/1024/1024:.1f}MB)")
            return True

    # 断点续传准备
    part_path = save_path + ".part"
    downloaded = 0
    if os.path.exists(part_path):
        downloaded = os.path.getsize(part_path)
    elif os.path.exists(save_path) and expected_size and os.path.getsize(save_path) < expected_size:
        try:
            os.rename(save_path, part_path)
            downloaded = os.path.getsize(part_path)
        except Exception:
            pass

    if downloaded > 0:
        pct = downloaded * 100 // expected_size if expected_size else 0
        print(f"📶 断点续传: {file_name} (已下载 {pct}%, {downloaded/1024/1024:.1f}MB)")

    for attempt in range(3):
        try:
            print(f"⬇ 开始下载{'（续传）' if downloaded else ''}: {file_name}")

            f = open(part_path, "ab")
            last_pct = -1
            chunk_count = 0
            try:
                async for chunk in client.iter_download(msg, offset=downloaded, request_size=1048576):
                    f.write(chunk)
                    downloaded += len(chunk)
                    await asyncio.sleep(0)
                    chunk_count += 1
                    if chunk_count % 5 == 0:
                        try:
                            await asyncio.wait_for(client.catch_up(), timeout=1)
                        except Exception:
                            pass
                    if expected_size and expected_size > 0:
                        pct = downloaded * 100 // expected_size
                        if pct >= last_pct + 5 or downloaded >= expected_size:
                            print(f"  ⏳ 下载中... {pct}%  ({downloaded/1024/1024:.1f}/{expected_size/1024/1024:.1f} MB)")
                            last_pct = pct
                f.flush()
            finally:
                f.close()

            # 下载完成 → 去掉 .part 后缀
            if os.path.exists(save_path):
                os.remove(save_path)
            os.rename(part_path, save_path)
            final_mb = os.path.getsize(save_path) / 1024 / 1024
            print(f"✅ 完成: {file_name} ({final_mb:.1f}MB)")
            return True

        except Exception as e:
            err_msg = str(e)
            # file reference 过期 → 刷新引用，不消耗重试次数
            if "file reference" in err_msg.lower() and "expired" in err_msg.lower():
                print(f"🔄 文件引用过期，刷新中...")
                try:
                    chat = None
                    if hasattr(msg, 'peer_id') and msg.peer_id:
                        chat = await client.get_input_entity(msg.peer_id)
                    elif hasattr(msg, 'chat_id') and msg.chat_id:
                        chat = msg.chat_id
                    elif hasattr(msg, 'chat') and msg.chat:
                        chat = msg.chat
                    if chat:
                        fresh = await client.get_messages(chat, ids=msg.id)
                        if fresh:
                            msg = fresh
                            if fresh.video:
                                expected_size = fresh.video.size
                            elif fresh.document:
                                expected_size = fresh.document.size
                            print(f"   引用已刷新，降速重试...")
                            await asyncio.sleep(10)
                            continue
                except Exception as re:
                    print(f"   刷新失败: {re}")
            print(f"⚠️ 下载失败 (尝试 {attempt+1}/3): {err_msg}")
            if attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"   {wait}s 后重试...")
                await asyncio.sleep(wait)
            else:
                print(f"❌ 下载失败（已达最大重试，.part 已保留）: {file_name}")
                return False
    return False


# ====== 消息事件处理 ======


@client.on(events.NewMessage(chats=['me']))
async def handler(event):
    """处理 Saved Messages：链接下载 或 视频/文件下载"""
    msg = event.message

    # === 场景 1: 纯文本 t.me 链接 → 解析并下载 ===
    if msg.message and not msg.video and not msg.document:
        parsed = parse_tg_link(msg.message)
        if parsed:
            chat, msg_id = parsed
            try:
                target = await client.get_messages(chat, ids=msg_id)
                if not target:
                    print(f"⚠️ 消息不存在: {msg.message}")
                    return
                if not (target.video or target.document):
                    print(f"⚠️ 该消息不含视频/文件: {msg.message}")
                    return

                file_name = get_file_name(target)
                if not file_name:
                    ext = '.mp4'
                    ext = f".{target.document.id}" if target.document else f".{target.video.id}"
                    file_name = f"link_video{ext}"

                print(f"🔗 链接下载: {file_name}")
                if target.message:
                    print(f"   来源: {target.message.strip()[:100]}")
                save_path = os.path.join(DOWNLOAD_PATH, file_name)

                # 查重
                fid = get_file_id(target)
                if fid and fid in load_ids():
                    if os.path.exists(save_path):
                        print(f"⏭  已下载过（ID:{fid}），跳过: {file_name}")
                        return
                    else:
                        s = load_ids(); s.discard(fid); save_ids(s)
                        print(f"📁 文件已删，重新下载")

                # 记录映射（用于断点续传）
                try:
                    m = load_map()
                    m[file_name] = {"chat": chat if isinstance(chat, str) else str(chat), "msg_id": msg_id}
                    save_map(m)
                except Exception:
                    pass

                ok = await download_with_progress(target, save_path, file_name)
                if ok and fid:
                    s = load_ids(); s.add(fid); save_ids(s)
                if ok:
                    try:
                        m = load_map(); m.pop(file_name, None); save_map(m)
                    except Exception:
                        pass
                if not ok:
                    print(f"❌ 链接下载失败: {file_name}")
            except Exception as e:
                print(f"❌ 链接下载失败: {e}")
            return

    # === 场景 2: 转发来的视频/文件 → 直接下载 ===
    if msg.video or msg.document:
        file_name = get_file_name(msg)
        if not file_name:
            return

        fid = get_file_id(msg)
        save_path = os.path.join(DOWNLOAD_PATH, file_name)
        if fid and fid in load_ids():
            if os.path.exists(save_path):
                print(f"⏭  已下载过（ID:{fid}），跳过: {file_name}")
                return
            else:
                s = load_ids(); s.discard(fid); save_ids(s)
                print(f"📁 文件已删，重新下载")

        try:
            print(f"⬇ 下载: {file_name}")
            if msg.message:
                print(f"   说明: {msg.message.strip()[:100]}")
            ok = await download_with_progress(msg, save_path, file_name)
            if ok and fid:
                s = load_ids(); s.add(fid); save_ids(s)
            elif not ok:
                try:
                    m = load_map()
                    m[file_name] = {"chat": "me", "msg_id": msg.id}
                    save_map(m)
                except Exception:
                    pass
        except Exception as e:
            print(f"❌ 失败: {e}")


# ====== QR 码登录 ======


async def try_qr_login():
    """通过 QR 码登录，不触发短信验证码。超时自动重试"""
    retry = 0
    max_retry = 2
    while retry <= max_retry:
        print(f"📱 生成 QR 登录码...（尝试 {retry+1}/{max_retry+1}）")
        if not client.is_connected():
            print("[*] 重新连接...")
            try:
                await client.connect()
            except Exception as e:
                print(f"[warn] 连接失败: {e}，等待后重试...")
                await asyncio.sleep(10)
                retry += 1
                continue
        qr = await client.qr_login()

        qr_url = qr.url
        print(f"QR URL: {qr_url}")
        img = qrcode.make(qr_url)
        qr_path = os.path.join(DOWNLOAD_PATH, "tg-bot-login-qr.png")
        img.save(qr_path)
        print(f"📱 QR 码已保存到: {qr_path}")
        print("请用 Telegram 手机端扫描此二维码完成登录（120 秒内）...")

        try:
            await qr.wait(timeout=120)
            print("✅ QR 登录成功！")
            return
        except asyncio.TimeoutError:
            print("[warn] QR 码已过期")
            await client.disconnect()
            retry += 1
            if retry <= max_retry:
                print("[*] 10 秒后重新生成 QR 码...")
                await asyncio.sleep(10)
            continue

    print("[error] QR 登录失败（已达最大重试次数），请检查网络和代理后重启容器")
    try:
        qr_path = os.path.join(DOWNLOAD_PATH, "tg-bot-login-qr.png")
        if os.path.exists(qr_path):
            os.remove(qr_path)
    except Exception:
        pass
    raise RuntimeError("QR 登录失败")


# ====== Bot 启动 ======


async def start_bot():
    """启动 bot：先建立 TCP 连接，再检查 session"""
    qr_file = os.path.join(DOWNLOAD_PATH, "tg-bot-login-qr.png")
    if os.path.exists(qr_file):
        try:
            os.remove(qr_file)
            print("🗑️ 已清理残留登录 QR 码")
        except Exception:
            pass

    os.makedirs(DOWNLOAD_PATH, exist_ok=True)

    connected = False
    for attempt in range(3):
        try:
            await client.connect()
            connected = True
            break
        except Exception as e:
            print(f"[warn] 连接 TG 失败 (尝试 {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(5)

    if not connected:
        print("[warn] 3 次连接均失败，进入 QR 登录")
        await try_qr_login()
        if not client.is_connected():
            await client.connect()
    else:
        try:
            if await client.is_user_authorized():
                print("✅ Session 有效，直接登录")
            else:
                print("[warn] Session 无效，进入 QR 登录")
                await client.disconnect()
                await try_qr_login()
        except Exception as e:
            print(f"[warn] Session 检查失败: {e}，进入 QR 登录")
            await client.disconnect()
            await try_qr_login()

    print("✅ Bot 已启动！转发视频到「已保存消息」自动下载")
    print("   💡 发送 t.me 频道消息链接也可下载（支持禁止转发的频道）")
    print(f"   下载目录: {DOWNLOAD_PATH}")
    if os.path.exists(qr_file):
        try:
            os.remove(qr_file)
            print("🗑️ 已清理登录 QR 码")
        except Exception:
            pass


# ====== 自动续传（启动扫描） ======


async def resume_interrupted():
    """启动时扫描，自动续传中断的下载"""
    print("\n🔍 扫描中断下载...")

    local_files = {}
    for f in os.listdir(DOWNLOAD_PATH):
        path = os.path.join(DOWNLOAD_PATH, f)
        if os.path.isfile(path):
            local_files[f] = os.path.getsize(path)

    found = 0
    resumed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    # 优先使用下载映射直接定位
    dl_map = load_map()
    if dl_map:
        print(f"  📋 映射表中有 {len(dl_map)} 个未完成下载，直接恢复...")
        for file_name, info in list(dl_map.items()):
            if file_name not in local_files and file_name + ".part" not in local_files:
                dl_map.pop(file_name, None)
                continue
            try:
                target = await client.get_messages(info["chat"], ids=info["msg_id"])
                if target and (target.video or target.document):
                    save_path = os.path.join(DOWNLOAD_PATH, file_name)
                    print(f"  🗺️ 映射恢复: {file_name}")
                    ok = await download_with_progress(target, save_path, file_name)
                    await asyncio.sleep(0.5)
                    if ok:
                        resumed += 1
                        dl_map.pop(file_name, None)
                    else:
                        found += 1
                else:
                    dl_map.pop(file_name, None)
            except Exception as e:
                print(f"  ⚠️ 映射恢复失败: {file_name} - {e}")
        save_map(dl_map)
        if resumed > 0:
            print(f"  📋 映射恢复完成，续传了 {resumed} 个\n")

    # 再扫描 Saved Messages
    print("🔍 扫描 Saved Messages...")
    try:
        async for msg in client.iter_messages("me", limit=200):
            if msg.date and msg.date.replace(tzinfo=timezone.utc) < cutoff:
                break

            target = None
            if msg.video or msg.document:
                target = msg
                caption = (msg.message or "").strip()
                name = (sanitize_filename(caption) + ".mp4") if caption and len(caption) >= 2 else f"video_{msg.id}.mp4"
            elif msg.message and 't.me/' in msg.message:
                parsed = parse_tg_link(msg.message)
                if parsed:
                    try:
                        chat_id, msg_id = parsed
                        target = await client.get_messages(chat_id, ids=msg_id)
                        if target and (target.video or target.document):
                            caption = (target.message or "").strip()
                            name = (sanitize_filename(caption) + ".mp4") if caption and len(caption) >= 2 else f"video_{msg_id}.mp4"
                        else:
                            target = None
                    except Exception:
                        continue

            if target is None:
                continue

            found += 1
            expected = target.video.size if target.video else target.document.size if target.document else None

            if name in local_files and expected and local_files[name] == expected:
                continue

            save_path = os.path.join(DOWNLOAD_PATH, name)
            part_path = save_path + ".part"
            if not os.path.exists(save_path) and not os.path.exists(part_path):
                name_prefix = name[:20]
                matched = False
                for f in os.listdir(DOWNLOAD_PATH):
                    if f.endswith('.part') and f[:20] == name_prefix:
                        save_path = os.path.join(DOWNLOAD_PATH, f[:-5])
                        part_path = save_path + ".part"
                        print(f"  🔗 模糊匹配: {f}")
                        matched = True
                        break
                if not matched:
                    await asyncio.sleep(0.1)
                    continue

            ok = await download_with_progress(target, save_path, name)
            try:
                await asyncio.wait_for(client.catch_up(), timeout=3)
            except Exception:
                pass
            if ok:
                resumed += 1
                local_files[name] = os.path.getsize(save_path)

    except Exception as e:
        print(f"  ⚠️ 扫描中断: {e}")

    print(f"  📋 扫描了 {found} 个，续传了 {resumed} 个\n")


# ====== 主循环 ======


async def main():
    """主循环：断开后手动重连"""
    while True:
        try:
            await start_bot()
            await resume_interrupted()
            print("[*] Bot 运行中，等待消息...")
            await client.disconnected
            print("[warn] 连接已断开，5s 后自动重连...")
        except KeyboardInterrupt:
            print("⏹ 收到停止信号，退出中...")
            break
        except Exception as e:
            print(f"[error] 发生错误: {e}，5s 后重试...")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(5)

    print("✅ 已退出")


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        print("❌ 请设置环境变量 TG_API_ID 和 TG_API_HASH")
        sys.exit(1)
    asyncio.run(main())
