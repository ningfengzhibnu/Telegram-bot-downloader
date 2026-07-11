"""
TG Bot 主文件 — 自动下载保存到 NAS
"""

import os
import asyncio
import sys
import re
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import urllib.request
import urllib.parse

import qrcode
from PIL import Image

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
DOWNLOAD_PATH = "/downloads/videos"
MAP_PATH = "/downloads/download_map.json"
IDS_PATH = "/downloads/downloaded_ids.json"

# 下载映射：记录 t.me 链接的来源，用于断点续传时直接定位
def load_map():
    import json
    try:
        with open(MAP_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_map(m):
    import json
    with open(MAP_PATH, "w") as f:
        json.dump(m, f)

# 已下载 ID 记录：防止重复下载同一视频
def load_ids():
    import json
    try:
        with open(IDS_PATH) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_ids(s):
    import json
    with open(IDS_PATH, "w") as f:
        json.dump(list(s), f)

PROXY_HOST = os.environ.get("TG_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("TG_PROXY_PORT", "7890"))
PROXY_TYPE = os.environ.get("TG_PROXY_TYPE", "socks5")
PROXY = (PROXY_TYPE, PROXY_HOST, PROXY_PORT) if PROXY_HOST else None

SESSION_PATH = "/downloads/session"
client = TelegramClient(SESSION_PATH, API_ID, API_HASH, proxy=PROXY)

# 用于自动续传的追踪：记录每个文件的上次尝试时间和总尝试次数
retry_tracker = {}  # file_name -> {"last_try": timestamp, "count": int}


# ======== 下载函数 ========


# 活跃下载追踪 + 并发限制
_active_downloads = set()
DL_SEMAPHORE = asyncio.Semaphore(2)  # 最大并发下载 2（>2 会触发 TG 服务器流控掐断连接）

async def auto_resume_scheduler():
    """后台调度：每 5 分钟扫描 .part 文件，自动并发续传"""
    while True:
        await asyncio.sleep(300)
        await _do_resume_scan()

async def _do_resume_scan():
    """扫描 .part 文件，并发续传（最多 DL_SEMAPHORE 个同时下载）"""
    dl_map = load_map()
    if not dl_map:
        return

    part_files = {}
    try:
        for f in os.listdir(DOWNLOAD_PATH):
            if f.endswith('.part'):
                base = f[:-5]
                part_size = os.path.getsize(os.path.join(DOWNLOAD_PATH, f))
                part_files[base] = part_size
    except Exception:
        return

    if not part_files:
        return

    now = asyncio.get_event_loop().time()
    tasks = []
    for file_name, part_size in part_files.items():
        if file_name not in dl_map or file_name in _active_downloads:
            continue

        # 固定间隔检查：每10分钟重试一次，永不放弃
        if file_name in retry_tracker:
            tr = retry_tracker[file_name]
            if now - tr.get("last_try", 0) < 600:
                continue

        info = dl_map[file_name]
        task = asyncio.create_task(_try_resume_one(file_name, part_size, info, now))
        tasks.append(task)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        save_map(load_map())

async def _try_resume_one(file_name, part_size, info, now):
    """单个文件续传（受 DL_SEMAPHORE 并发控制）"""
    async with DL_SEMAPHORE:
        _active_downloads.add(file_name)
        try:
            target = await client.get_messages(info["chat"], ids=info["msg_id"])
            if not target or not (target.video or target.document):
                m = load_map(); m.pop(file_name, None); save_map(m)
                return
            if hasattr(target.media, 'webpage') and target.media.webpage:
                # 消息被TG解析为网页预览，不删.part文件（可能是临时异常）
                # 只从映射表移除，让后续孤儿扫描重新匹配
                print(f"  ⚠️ 映射恢复: {file_name} 消息变为webpage，跳过(保留.part)")
                m = load_map(); m.pop(file_name, None); save_map(m)
                return

            print(f"\n🕐 自动续传: {file_name} ({part_size/1024/1024:.1f}MB)")
            retry_tracker[file_name] = {"last_try": now, "count": retry_tracker.get(file_name, {}).get("count", 0) + 1}

            save_path = os.path.join(DOWNLOAD_PATH, file_name)
            ok = await download_with_progress(target, save_path, file_name)
            if ok:
                retry_tracker.pop(file_name, None)
                m = load_map(); m.pop(file_name, None); save_map(m)
            else:
                retry_tracker[file_name]["last_try"] = now
        except Exception as e:
            print(f"  ⚠️ 续传异常: {file_name} - {e}")
            if file_name in retry_tracker:
                retry_tracker[file_name]["last_try"] = now
            else:
                retry_tracker[file_name] = {"last_try": now}
        finally:
            _active_downloads.discard(file_name)



async def download_with_progress(msg, save_path, file_name, sent_msg=None):
    """带进度条的单线程下载，支持断点续传。
    sent_msg: 可选 Telegram 消息对象，下载进度会编辑到这条消息中"""
    # 获取 TG 消息里的真实文件大小
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

    # 检查已完整 -> 跳过
    if os.path.exists(save_path):
        local_size = os.path.getsize(save_path)
        if local_size == 0:
            os.remove(save_path)
            print(f"\U0001f5d1  删除空文件: {file_name}")
        elif expected_size and abs(local_size - expected_size) <= max(5242880, expected_size * 0.05):
            print(f"\u23ed  跳过（已完整）: {file_name} ({local_size/1024/1024:.1f}MB)")
            if sent_msg:
                try:
                    await sent_msg.edit(f"\u2705 {file_name} ({local_size/1024/1024:.1f}MB)")
                except Exception:
                    pass
            return True

    # 断点续传检查
    part_file = save_path + ".part"
    downloaded = 0
    if os.path.exists(part_file):
        downloaded = os.path.getsize(part_file)
        if downloaded > 0:
            pct = downloaded * 100 // expected_size if expected_size else 0
            print(f"\U0001f4f6 断点续传: {file_name} (已下载 {pct}%, {downloaded/1024/1024:.1f}MB)")

    for attempt in range(3):
        try:
            # 重试时不删 .part，保留已下载数据用于续传
            if attempt > 0:
                # 检查 part_file 是否还有有效数据可以续传
                if os.path.exists(part_file):
                    downloaded = os.path.getsize(part_file)
                else:
                    downloaded = 0
                print(f"\U0001f504 重试 (attempt {attempt+1}/3): {file_name} (续传 {downloaded/1024/1024:.1f}MB)")

            print(f"\u2b07 开始下载{'（续传）' if downloaded > 0 else ''}: {file_name}")

            last_pct = -1
            if downloaded > 0:
                f = open(part_file, "ab")  # 续传模式
            else:
                f = open(part_file, "wb")  # 覆盖写入
            dl_size = downloaded
            max_stream_retries = 3
            stream_retry = 0

            while dl_size < (expected_size or float("inf")):
                try:
                    offset = dl_size
                    it = client.iter_download(msg, offset=offset, request_size=1048576).__aiter__()
                    while dl_size < (expected_size or float("inf")):
                        try:
                            chunk_data = await asyncio.wait_for(it.__anext__(), timeout=120)
                        except (StopAsyncIteration, asyncio.TimeoutError):
                            break
                        f.write(chunk_data)
                        dl_size += len(chunk_data)
                        if expected_size and expected_size > 0:
                            pct = dl_size * 100 // expected_size
                            if pct >= last_pct + 5 or dl_size >= expected_size:
                                bar_len = 20
                                filled = bar_len * pct // 100
                                bar = chr(0x2588) * filled + chr(0x2591) * (bar_len - filled)
                                dl_mb = dl_size / 1048576
                                total_mb = expected_size / 1048576
                                print(f"  [{bar}] {pct}%  ({dl_mb:.1f}/{total_mb:.1f} MB)")
                                if sent_msg:
                                    try:
                                        await sent_msg.edit(f"[{bar}] {pct}%  ({dl_mb:.1f}/{total_mb:.1f} MB)")
                                    except Exception:
                                        pass
                                last_pct = pct
                        await asyncio.sleep(0)
                except Exception:
                    pass
                if expected_size and dl_size >= expected_size:
                    break
                stream_retry += 1
                if stream_retry > max_stream_retries:
                    raise Exception(f"下载流中断 {max_stream_retries} 次，数据不完整: {dl_size}/{expected_size}")
                print(f"  ⚡ 下载流中断，{stream_retry}/{max_stream_retries}次，3s后恢复续传... (已下载 {dl_size/1048576:.1f}MB)")
                f.flush()
                await asyncio.sleep(3)

            f.flush()
            f.close()

            # 下载完成，重命名 .part -> 最终文件
            try:
                bak_path = save_path + ".bak"
                if os.path.exists(save_path):
                    os.rename(save_path, bak_path)
                os.rename(part_file, save_path)
                if os.path.exists(bak_path):
                    os.remove(bak_path)
            except FileNotFoundError:
                # 并发下载时 .part 已被另一个进程 rename，检查 .mp4 是否已就绪
                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    f.close()
                    return True
                raise

            # FUSE sync 等待
            await asyncio.sleep(0.5)

            final_size = os.path.getsize(save_path)
            if final_size == 0:
                raise Exception(f"下载后文件为空: {file_name}")
            if expected_size and abs(final_size - expected_size) > max(5242880, expected_size * 0.05):
                raise Exception(f"下载后大小不匹配: {final_size} != {expected_size}")

            final_mb = final_size / 1024 / 1024
            print(f"\u2705 完成: {file_name} ({final_mb:.1f}MB)")
            if sent_msg:
                try:
                    await sent_msg.edit(f"\u2705 {file_name} ({final_mb:.1f}MB)")
                except Exception:
                    pass

            # 清理映射
            try:
                m = load_map()
                if m.pop(file_name, None):
                    save_map(m)
            except Exception:
                pass
            return True

        except Exception as e:
            err_msg = str(e)
            if "server closed the connection" in err_msg.lower():
                print(f"\U0001f50c TG 服务器断流，{5*(attempt+1)}s 后重试... ({file_name})")
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if "file reference" in err_msg.lower() and "expired" in err_msg.lower():
                print(f"\U0001f504 文件引用过期，刷新中...")
                try:
                    chat = None
                    if hasattr(msg, "peer_id") and msg.peer_id:
                        chat = await client.get_input_entity(msg.peer_id)
                    elif hasattr(msg, "chat_id") and msg.chat_id:
                        chat = msg.chat_id
                    elif hasattr(msg, "chat") and msg.chat:
                        chat = msg.chat
                    if chat:
                        fresh = await client.get_messages(chat, ids=msg.id)
                        if fresh:
                            msg = fresh
                            if fresh.video:
                                expected_size = fresh.video.size
                            elif fresh.document:
                                expected_size = fresh.document.size
                            print("   引用已刷新，降速重试...")
                            await asyncio.sleep(10)
                            continue
                except Exception as re:
                    print(f"   刷新失败: {re}")
            if "MessageMediaWebPage" in err_msg or "InputFileLocation" in err_msg:
                print(f"\u274c 消息不含可下载的视频/文件（网页预览），放弃: {file_name}")
                return False
            print(f"\u26a0\ufe0f 下载失败 (尝试 {attempt+1}/3): {err_msg}")
            # 如果 save_path 已存在且大小在容差内
            if os.path.exists(save_path) and expected_size:
                sz = os.path.getsize(save_path)
                if sz > 0 and abs(sz - expected_size) <= max(5242880, expected_size * 0.05):
                    print(f"\u2705 已完成（忽略报错）: {file_name}")
                    if sent_msg:
                        try:
                            await sent_msg.edit(f"\u2705 {file_name} ({os.path.getsize(save_path)/1024/1024:.1f}MB)")
                        except Exception:
                            pass
                    if os.path.exists(part_file):
                        os.remove(part_file)
                    return True
            if attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"   {wait}s 后重试...")
                await asyncio.sleep(wait)
            else:
                print(f"\u274c 下载失败（已达最大重试）: {file_name}")
                if sent_msg:
                    try:
                        await sent_msg.edit(f"\u274c 下载失败: {file_name}")
                    except Exception:
                        pass
                return False
    return False

def sanitize_filename(name):
    """清理文件名中的非法字符"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.strip('. ')
    if len(name) > 200:
        name = name[:200]
    return name if name else None


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
        lines = msg.message.strip().split('\n')
        tags = lines[0].strip()
        desc = ' '.join(line.strip() for line in lines[1:] if line.strip())
        if desc:
            caption = sanitize_filename(f"{tags} {desc}")
        else:
            caption = sanitize_filename(tags)

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
        chat_id = int(f"-100{identifier}")
        return (chat_id, msg_id)
    else:
        return (identifier, msg_id)


async def _poll_x_dl_progress(sent_msg, url):
    import aiohttp
    last_text = None
    while True:
        await asyncio.sleep(8)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('http://192.168.3.2:8889/api/status', timeout=10) as resp:
                    data = await resp.json()
                    status = data.get('status', '')
                    if status == 'done':
                        fname = data.get('filename', 'Done')
                        await sent_msg.edit('Done: ' + fname + chr(10) + 'NAS /downloads/videos/')
                        return
                    elif status == 'fail':
                        msg = data.get('message', 'Unknown error')
                        await sent_msg.edit('Failed: ' + msg)
                        return
                    elif status == 'downloading':
                        pct = data.get('progress', 0)
                        speed = data.get('speed', '')
                        eta = data.get('eta', '')
                        bar_len = 20
                        filled = int(bar_len * pct / 100)
                        bar = '█' * filled + '░' * (bar_len - filled)
                        text = '[' + bar + '] ' + str(int(pct)) + '%'
                        if speed:
                            text += ' | ' + str(speed)
                        if eta:
                            text += ' | ETA ' + str(eta)
                        if text != last_text:
                            await sent_msg.edit(text)
                            last_text = text
        except Exception as e:
            print('Poll error: ' + str(e))
            continue


@client.on(events.NewMessage(chats=['me']))
async def handler(event):
    """处理 Saved Messages：链接下载 或 视频/文件下载"""
    msg = event.message

    # === 场景 0: /rename_old 命令 → 批量重命名纯标签文件 ===
    if msg.message and msg.message.strip().startswith('/rename_old'):
        await event.reply("🔍 正在搜索频道消息描述并重命名...")
        renamed = 0
        skipped = 0
        for f in os.listdir(DOWNLOAD_PATH):
            if not f.endswith('.mp4') or not f.startswith('#'):
                continue
            name_no_ext = f[:-4]
            # 纯标签文件名：只由 #tag1 #tag2 ... 组成，不含描述文本
            parts = name_no_ext.strip().split()
            if not all(p.startswith('#') for p in parts):
                continue  # 包含非标签文字（已有描述），跳过
            tags = name_no_ext.strip()
            try:
                found = None
                async for m in client.iter_messages("tyJ911", search=tags, limit=10):
                    if m.message and m.message.strip().split('\n')[0].strip() == tags:
                        found = m
                        break
                if found:
                    new_name = get_file_name(found)
                    if new_name and new_name != f:
                        old_path = os.path.join(DOWNLOAD_PATH, f)
                        new_path = os.path.join(DOWNLOAD_PATH, new_name)
                        if not os.path.exists(new_path):
                            os.rename(old_path, new_path)
                            print(f"  ✅ {f} -> {new_name}")
                            renamed += 1
                        else:
                            print(f"  ⚠️ 目标已存在: {new_name}")
                            skipped += 1
                    else:
                        skipped += 1
                else:
                    print(f"  ⚠️ 未匹配: {tags}")
                    skipped += 1
            except Exception as e:
                print(f"  ❌ {f}: {e}")
                skipped += 1
        await event.reply(f"✅ 完成！重命名 {renamed} 个，跳过 {skipped} 个")
        return

    # === 场景 1: 收到纯文本 t.me 链接 → 解析并下载 ===
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
                    if target.document:
                        file_name = f"{target.document.id}{ext}"
                    else:
                        file_name = f"{target.video.id}{ext}"

                print(f"🔗 链接下载: {file_name}")
                if target.message:
                    print(f"   来源: {target.message.strip()[:100]}")
                save_path = os.path.join(DOWNLOAD_PATH, file_name)

                # 查重：同一视频 ID 不重复下载，但文件被删了可以重下
                fid = get_file_id(target)
                if fid and fid in load_ids():
                    if os.path.exists(save_path):
                        print(f"⏭  已下载过（ID:{fid}），跳过: {file_name}")
                        return
                    else:
                        s = load_ids(); s.discard(fid); save_ids(s)
                        print(f"📁 文件已删，重新下载")

                # 记录来源映射，用于断点续传
                try:
                    m = load_map()
                    m[file_name] = {"chat": chat, "msg_id": msg_id}
                    save_map(m)
                except Exception:
                    pass

                if file_name in _active_downloads:
                    print(f"⏭  已在下载中，跳过: {file_name}")
                    return
                async with DL_SEMAPHORE:
                    _active_downloads.add(file_name)
                    try:
                        sent = await event.reply(f"⬇ {file_name}\n[░░░░░░░░░░░░░░░░░░░░] 0%")
                        # 更新映射表，记录进度条消息 ID
                        try:
                            m = load_map()
                            if file_name in m:
                                m[file_name]["sent_id"] = sent.id
                                save_map(m)
                        except Exception:
                            pass
                        ok = await download_with_progress(target, save_path, file_name, sent)
                    finally:
                        _active_downloads.discard(file_name)

                if ok:
                    # 记录已下载 ID，防止重复
                    if fid:
                        try:
                            s = load_ids(); s.add(fid); save_ids(s)
                        except Exception: pass
                if not ok:
                    print(f"❌ 链接下载失败: {file_name}")
                else:
                    # 下载完成，清除映射
                    try:
                        m = load_map()
                        m.pop(file_name, None)
                        save_map(m)
                    except Exception:
                        pass
            except Exception as e:
                print(f"❌ 链接下载失败: {e}")
            return

    # === 场景 1.5: X/Twitter 链接 → 转发到 NAS x-downloader ===
    if msg.message and ('x.com/' in msg.message or 'twitter.com/' in msg.message):
        urls = re.findall(r'https?://(?:x|twitter)\.com/\S+', msg.message)
        if urls:
            x_url = urls[0]
            try:
                # Submit to NAS x-downloader (port 8889)
                data = urllib.parse.urlencode({'url': x_url}).encode()
                req = urllib.request.Request(
                    'http://192.168.3.2:8889/',
                    data=data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'}
                )
                resp = urllib.request.urlopen(req, timeout=15)
                print(f'🐦 X-DL submit: {x_url}')
                sent = await event.reply(f'🐦 收到，正在下载：\n{x_url}\n⏳ 完成后会通知你（NAS 下载慢，可能要几分钟）。')
                # Start progress polling in background
                asyncio.create_task(_poll_x_dl_progress(sent, x_url))
            except Exception as e:
                print(f'❌ X-DL failed: {x_url} -> {e}')
                await event.reply(f'❌ 提交下载失败：{e}')
            return
        # 无效的 x.com 链接（没有匹配到 URL）
        await event.reply('⚠️ 检测到 X/Twitter 链接，但无法提取有效 URL')
        return

    # === 场景 2: 转发来的视频/文件 → 直接下载 ===
    if msg.video or msg.document:
        file_name = get_file_name(msg)
        if not file_name:
            return

        # 查重
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
            save_path = os.path.join(DOWNLOAD_PATH, file_name)
            sent = await event.reply(f"⬇ {file_name}\n[░░░░░░░░░░░░░░░░░░░░] 0%")
            ok = await download_with_progress(msg, save_path, file_name, sent)
            if ok and fid:
                try:
                    s = load_ids(); s.add(fid); save_ids(s)
                except Exception: pass
            elif not ok:
                # 下载未完成，记录映射
                try:
                    m = load_map()
                    m[file_name] = {"chat": "me", "msg_id": msg.id, "sent_id": sent.id}
                    save_map(m)
                except Exception: pass
        except Exception as e:
            print(f"❌ 失败: {e}")


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
        qr_path = "/downloads/tg-bot-login-qr.png"
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
        if os.path.exists("/downloads/tg-bot-login-qr.png"):
            os.remove("/downloads/tg-bot-login-qr.png")
    except Exception:
        pass
    raise RuntimeError("QR 登录失败")


async def start_bot():
    """启动 bot：先建立 TCP 连接，再检查 session"""
    _qr_file = "/downloads/tg-bot-login-qr.png"
    if os.path.exists(_qr_file):
        try:
            os.remove(_qr_file)
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
                # 服务端验证：is_user_authorized() 只看本地文件，auth_key desync 后仍返回 True
                # 调 get_me() 做真正的服务端校验
                try:
                    await client.get_me()
                    print("✅ Session 有效，直接登录")
                except Exception as e:
                    err = str(e)
                    if "key is not registered" in err.lower() or "unregistered" in err.lower():
                        print(f"[warn] Session 已失效（auth_key desync），进入 QR 登录")
                    else:
                        print(f"[warn] Session 验证失败: {e}，进入 QR 登录")
                    await client.disconnect()
                    await try_qr_login()
            else:
                print("[warn] Session 无效，进入 QR 登录")
                await client.disconnect()
                await try_qr_login()
        except Exception as e:
            print(f"[warn] Session 检查失败: {e}，进入 QR 登录")
            await client.disconnect()
            await try_qr_login()

    print("✅ Bot 已启动！转发视频到「已保存消息」自动下载到 NAS")
    print("   💡 发送 t.me 频道消息链接也可下载（支持禁止转发的频道）")
    print(f"   下载目录: {DOWNLOAD_PATH}")
    qr_file = "/downloads/tg-bot-login-qr.png"
    if os.path.exists(qr_file):
        os.remove(qr_file)
        print("🗑️ 已清理登录 QR 码")


async def _try_resume_one_dlmap(file_name, info):
    """启动时并发恢复单个 dl_map 条目"""
    async with DL_SEMAPHORE:
        _active_downloads.add(file_name)
        try:
            target = await client.get_messages(info["chat"], ids=info["msg_id"])
            if not target or not (target.video or target.document):
                m = load_map(); m.pop(file_name, None); save_map(m)
                return
            if hasattr(target.media, 'webpage') and target.media.webpage:
                # 消息被TG解析为网页预览，不删.part文件（可能是临时异常）
                # 只从映射表移除，让后续孤儿扫描重新匹配
                print(f"  ⚠️ 映射恢复: {file_name} 消息变为webpage，跳过(保留.part)")
                m = load_map(); m.pop(file_name, None); save_map(m)
                return
            print(f"  🗺️ 映射恢复: {file_name}")

            save_path = os.path.join(DOWNLOAD_PATH, file_name)

            # 尝试恢复之前的进度条消息
            sent = None
            sent_id = info.get("sent_id")
            if sent_id:
                try:
                    sent = await client.get_messages("me", ids=sent_id)
                except Exception:
                    pass

            ok = await download_with_progress(target, save_path, file_name, sent)
            if ok:
                m = load_map(); m.pop(file_name, None); save_map(m)
        except Exception as e:
            print(f"  ⚠️ 映射恢复失败: {file_name} - {e}")
        finally:
            _active_downloads.discard(file_name)


async def resume_interrupted():
    """启动时扫描 Saved Messages，自动续传中断的下载"""
    from datetime import datetime, timezone, timedelta
    print("\n🔍 扫描 Saved Messages 查找中断下载...")

    # 获取本地已有文件（包括 .part 和完整文件）
    local_files = {}  # filename -> size
    for f in os.listdir(DOWNLOAD_PATH):
        path = os.path.join(DOWNLOAD_PATH, f)
        if os.path.isfile(path):
            local_files[f] = os.path.getsize(path)

    found = 0
    resumed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    parsed = None
    chat_id = None
    msg_id = None

    # === 优先使用下载映射直接定位（避免扫描 Saved Messages）===
    dl_map = load_map()
    if dl_map:
        print(f"  📋 映射表中有 {len(dl_map)} 个未完成下载，并发恢复...")
        tasks = []
        for file_name, info in list(dl_map.items()):
            if file_name not in local_files and file_name + ".part" not in local_files:
                dl_map.pop(file_name, None)
                continue
            task = asyncio.create_task(_try_resume_one_dlmap(file_name, info))
            tasks.append(task)
            if len(tasks) >= 5:
                await asyncio.gather(*tasks, return_exceptions=True)
                tasks = []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        save_map(dl_map)

    # === 再扫描 Saved Messages 处理映射表里没有的 ===
    print("🔍 扫描 Saved Messages 查找中断下载...")

    try:
        async for msg in client.iter_messages("me", limit=200):
            if msg.date and msg.date.replace(tzinfo=timezone.utc) < cutoff:
                break

            target = None  # 要下载的消息

            # 情况 1: 直接转发的视频
            if msg.video or msg.document:
                target = msg
                caption = (msg.message or "").strip()
                if caption and len(caption) >= 2:
                    name = sanitize_filename(caption) + ".mp4"
                else:
                    name = f"video_{msg.id}.mp4"

            # 情况 2: t.me 链接（禁止转发频道的消息）
            # 也覆盖 webpage 消息（TG 可能把链接转成网页预览导致 media 变成 webpage）
            elif msg.message and 't.me/' in msg.message:
                parsed = parse_tg_link(msg.message)
                if parsed:
                    try:
                        chat_id, msg_id = parsed
                        target = await client.get_messages(chat_id, ids=msg_id)
                        if target and (target.video or target.document):
                            caption = (target.message or "").strip()
                            if caption and len(caption) >= 2:
                                name = sanitize_filename(caption) + ".mp4"
                            else:
                                name = f"video_{msg_id}.mp4"
                        else:
                            target = None
                    except Exception:
                        continue

            if target is None:
                continue

            found += 1
            expected = target.video.size if target.video else target.document.size if target.document else None

            # 检查是否完整
            if name in local_files and expected and local_files[name] == expected:
                # 尝试找到之前的进度条消息并更新为完成
                try:
                    final_mb = local_files[name] / 1048576
                    async for reply in client.iter_messages("me", reply_to=msg.id, limit=1):
                        if reply.out and ("⬇" in (reply.text or "") or "[" in (reply.text or "")):
                            await reply.edit(f"✅ {name} ({final_mb:.1f}MB)")
                            break
                    else:
                        await client.send_message("me", f"✅ 已完成: {name} ({final_mb:.1f}MB)")
                except Exception:
                    pass
                continue  # 已完整，跳过

            # 模糊匹配已有的 .part 文件（防止 TG 消息文字微变导致文件名不同）
            save_path = os.path.join(DOWNLOAD_PATH, name)
            part_path = save_path + ".part"
            if not os.path.exists(save_path) and not os.path.exists(part_path):
                # 模糊匹配文件名（前20字）
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
                    await asyncio.sleep(0.1)  # 让出控制权
                    continue  # 没缓存，跳过

            # 尝试复用已有的进度条消息，或创建新的
            sent = None
            try:
                async for reply in client.iter_messages("me", reply_to=msg.id, limit=1):
                    if reply.out and ("⬇" in (reply.text or "") or "[" in (reply.text or "")):
                        sent = reply
                        break
            except Exception:
                pass
            if sent is None:
                try:
                    sent = await client.send_message("me", f"⬇ {name}\n[░░░░░░░░░░░░░░░░░░░░] 0%")
                except Exception:
                    pass

            ok = await download_with_progress(target, save_path, name, sent)
            # 处理积压的新消息
            try:
                await asyncio.wait_for(client.catch_up(), timeout=3)
            except Exception:
                pass
            if ok:
                resumed += 1
                local_files[name] = os.path.getsize(save_path)
            elif name not in local_files:
                # 下载未完成，记录映射方便下次续传（转发和链接都记）
                try:
                    m = load_map()
                    if parsed:
                        m[name] = {"chat": chat_id, "msg_id": msg_id, "sent_id": sent.id if sent else None}
                    else:
                        m[name] = {"chat": "me", "msg_id": msg.id, "sent_id": sent.id if sent else None}
                    save_map(m)
                except Exception:
                    pass

    except Exception as e:
        print(f"  ⚠️ 扫描中断: {e}")

    # 报告孤儿 .part 文件（不在映射表里，也未匹配到消息）
    dl_map = load_map()
    orphan_parts = [f for f in os.listdir(DOWNLOAD_PATH) if f.endswith('.part') and f[:-5] not in dl_map]
    if orphan_parts:
        print(f"  ⚠️ {len(orphan_parts)} 个孤儿 .part 未匹配: {', '.join(orphan_parts)}")
    print(f"  📋 扫描了 {found} 个视频，续传了 {resumed} 个\n")

async def main():
    """主循环：断开后手动重连，不走 run_until_disconnected 的自动重连"""
    # 启动自动续传调度器（后台任务）
    resume_task = asyncio.create_task(auto_resume_scheduler())
    print("🕐 自动续传调度器已启动（每5分钟扫描 .part 文件）")
    
    try:
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
    finally:
        resume_task.cancel()
        try:
            await resume_task
        except asyncio.CancelledError:
            pass

    print("✅ 已退出")


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        print("❌ 请设置环境变量 TG_API_ID 和 TG_API_HASH")
        sys.exit(1)
    asyncio.run(main())
