import aiohttp
import aiofiles
import json
import os
import asyncio
import time
import hashlib
from typing import Dict, List, Union, Any
from PIL import Image, UnidentifiedImageError
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Node, Plain, Image as CompImage
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.all import AstrBotConfig
from astrbot.api import logger

# 自定义异常类
class NoGameFound(Exception): pass
class DownloadNotFound(Exception): pass
class APIError(Exception): pass
class ImageProcessingError(Exception): pass

# 检查是否支持AVIF格式

from pillow_avif import AvifImagePlugin
AVIF_SUPPORT = True
logger.info("AVIF格式支持已启用")


# 创建临时缓存文件夹
TEMP_DIR = os.path.join(os.path.dirname(__file__), "tmp")
os.makedirs(TEMP_DIR, exist_ok=True)

"""TouchGal API接口封装"""
class TouchGalAPI:
    def __init__(self):
        self.base_url = "https://www.touchgal.us/api"
        self.search_url = f"{self.base_url}/search"
        self.download_url = f"{self.base_url}/patch/resource"
        
    async def search_game(self, keyword: str, limit: int = 15) -> List[Dict[str, Any]]:
        """搜索游戏信息"""
        headers = {"Content-Type": "application/json"}
        
        # 正确构造queryString参数（字符串格式的JSON数组）
        query_string = json.dumps([{"type": "keyword", "name": keyword}])
        
        payload = {
            "queryString": query_string,  # 使用字符串格式的JSON
            "limit": limit,
            "searchOption": {
                "searchInIntroduction": True,
                "searchInAlias": True,
                "searchInTag": True
            },
            "page": 1,
            "selectedType": "all",
            "selectedLanguage": "all",
            "selectedPlatform": "all",
            "sortField": "resource_update_time",
            "sortOrder": "desc",
            "selectedYears": ["all"],  # 添加缺失的必需字段
            "selectedMonths": ["all"]  # 添加缺失的必需字段
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.search_url, 
                    json=payload, 
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as response:
                    # 确保响应状态为200
                    if response.status != 200:
                        error_text = await response.text()
                        raise APIError(f"API请求失败: {response.status} - {error_text}")
                    
                    # 尝试解析JSON
                    try:
                        data = await response.json()
                    except Exception as e:
                        text_response = await response.text()
                        logger.error(f"JSON解析失败: {str(e)} - 响应内容: {text_response[:200]}")
                        raise APIError("API返回了无效的JSON数据")
                    
                    # 验证数据结构
                    if not isinstance(data, dict) or "galgames" not in data:
                        logger.warning(f"API返回了意外的数据结构: {data}")
                        raise APIError("API返回了无效的数据结构")
                    
                    if not data.get("galgames"):
                        raise NoGameFound(f"未找到游戏: {keyword}")
                    
                    return data["galgames"]
        except aiohttp.ClientError as e:
            raise APIError(f"网络请求错误: {str(e)}")

    async def get_downloads(self, patch_id: Union[int, str]) -> List[Dict[str, Any]]:
        """获取游戏下载资源"""
        params = {"patchId": patch_id}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.download_url, 
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise APIError(f"API请求失败: {response.status} - {error_text}")
                    
                    # 尝试解析JSON
                    try:
                        data = await response.json()
                    except Exception as e:
                        text_response = await response.text()
                        logger.error(f"JSON解析失败: {str(e)} - 响应内容: {text_response[:200]}")
                        raise APIError("API返回了无效的JSON数据")
                    
                    # 验证数据结构
                    if not isinstance(data, list):
                        logger.warning(f"API返回了意外的数据结构: {data}")
                        raise APIError("API返回了无效的数据结构")
                    
                    if not data:
                        raise DownloadNotFound(f"未找到ID为{patch_id}的下载资源")
                    
                    return data
        except aiohttp.ClientError as e:
            raise APIError(f"网络请求错误: {str(e)}")
    
    async def download_and_convert_image(self, url: str) -> Union[str, None]:
        """
        下载并转换图片为JPG格式
        支持AVIF格式转换（如果安装了pillow-avif-plugin）
        """
        if not url:
            return None
            
        # 生成唯一的文件名（使用URL的MD5避免重复下载）
        url_hash = hashlib.md5(url.encode()).hexdigest()
        filepath = os.path.join(TEMP_DIR, f"main_{url_hash}")
        output_path = os.path.join(TEMP_DIR, f"converted_{url_hash}.jpg")
        
        # 如果已经转换过，直接返回
        if os.path.exists(output_path):
            return output_path
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"获取图片失败: {response.status} - {url}")
                        return None
                    
                    # 检查图片类型
                    content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                    
                    # 写入原始图片
                    async with aiofiles.open(filepath, "wb") as f:
                        await f.write(await response.read())
                    
                    # 处理图片转换
                    return await self._convert_image(filepath, output_path)
                    
        except Exception as e:
            logger.warning(f"图片处理失败: {str(e)} - {url}")
            return None
        finally:
            # 清理原始文件
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception as e:
                    logger.warning(f"删除原始图片失败: {str(e)}")
    
    async def _convert_image(self, input_path: str, output_path: str) -> str:
        """转换图片为JPG格式"""
        try:
            # 在异步环境中处理图片转换
            def convert_image():
                with Image.open(input_path) as img:
                    # 转换为RGB模式（JPG需要）
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    
                    # 调整图片大小（避免过大）
                    max_size = (800, 800)
                    img.thumbnail(max_size, Image.LANCZOS)
                    
                    # 保存为JPG
                    img.save(output_path, "JPEG", quality=85)
                return output_path
            
            # 在线程池中执行同步的图片处理
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, convert_image)
        except UnidentifiedImageError:
            # 如果是AVIF格式但未安装支持库
            if AVIF_SUPPORT:
                logger.warning(f"无法识别的图片格式: {input_path}")
            else:
                logger.warning("检测到AVIF格式但未安装支持库，无法转换")
            return None
        except Exception as e:
            logger.warning(f"图片转换失败: {str(e)}")
            return None

@register(
    "astrbot_plugin_touchgal",
    "CCYellowStar2",
    "基于TouchGal API的Galgame信息查询与下载插件",
    "1.0",
    "https://github.com/CCYellowStar2/astrbot_plugin_touchgal"
)
class TouchGalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.search_limit = self.config.get("search_limit", 15)
        self.user_cache = {}  # 用户缓存: {user_id: {game_id: game_info}}
        self.api = TouchGalAPI()
        
        # 清理旧缓存
        self.cleanup_old_cache()

    def cleanup_old_cache(self):
        """清理旧的缓存图片"""
        try:
            for filename in os.listdir(TEMP_DIR):
                if filename.startswith("converted_") or filename.startswith("main_"):
                    file_path = os.path.join(TEMP_DIR, filename)
                    # 删除超过1天的缓存文件
                    if os.path.getmtime(file_path) < time.time() - 86400:
                        os.remove(file_path)
                        logger.info(f"清理旧缓存: {filename}")
        except Exception as e:
            logger.warning(f"清理缓存失败: {str(e)}")

    def _format_game_info(self, game_info: Dict[str, Any]) -> str:
        """格式化游戏信息"""
        # 处理标签
        tags = ", ".join(game_info.get("tags", []))
        if len(tags) > 100:  # 防止标签过长
            tags = tags[:97] + "..."
            
        # 处理平台
        platforms = ", ".join(game_info.get("platform", []))
        
        # 处理日期
        created_date = game_info.get("created", "")[:10]
        
        return (
            f"🆔 游戏ID: {game_info['id']}\n"
            f"🎮 名称: {game_info['name']}\n"
            f"🏷️ 标签: {tags}\n"
            f"📱 平台: {platforms}\n"
            f"⬇️ 下载次数: {game_info.get('download', 0)}\n"
            f"📅 添加日期: {created_date}\n"
            f"🔍 使用 '/下载gal {game_info['id']}' 获取下载地址"
        )

    def _format_downloads(self, downloads: List[Dict[str, Any]]) -> str:
        """格式化下载资源信息"""
        result = []
        for i, resource in enumerate(downloads, 1):
            # 确定平台类型
            if "windows" in resource["platform"]:
                platform = "💻 PC"
            elif "android" in resource["platform"]:
                platform = "📱 手机"
            else:
                platform = "🕹️ 其他"
                
            # 添加资源信息
            result.append(
                f"{i}. {platform}版: {resource['name']}\n"
                f"   📦 大小: {resource['size']}\n"
                f"   🔗 下载地址: {resource['content']}\n"
                f"      语言: {', '.join(resource['language'])}\n"
                f"   📝 备注: {resource['note'] or '无'}\n"
            )
        return "\n".join(result)

    @filter.command("查询gal")
    async def search_galgame(self, event: AstrMessageEvent):
        """查询Gal信息（包含封面图片）"""
        cmd = event.message_str.split(maxsplit=1)
        if len(cmd) < 2:
            yield event.plain_result("⚠️ 参数错误，请输入游戏名称")
            return

        keyword = cmd[1]
        user_id = event.get_sender_id()
        
        # 清空用户缓存
        self.user_cache.pop(user_id, None)
        
        try:
            yield event.plain_result(f"🔍 正在搜索: {keyword}")
            results = await self.api.search_game(keyword, self.search_limit)
            
            # 缓存游戏信息
            self.user_cache[user_id] = {game["id"]: game for game in results}
            
            # 并发下载所有封面图片
            cover_tasks = []
            for game in results:
                if game.get("banner"):
                    cover_tasks.append(self.api.download_and_convert_image(game["banner"]))
                else:
                    cover_tasks.append(None)  # 如果没有封面，添加None占位
            
            # 等待所有图片下载完成
            cover_paths = await asyncio.gather(*cover_tasks)
            
            # 构建消息链
            chain = []
            
            # 添加搜索结果标题
            response_lines = [f"🔍 找到 {len(results)} 个相关游戏:\n.."]
            chain.append(Plain(response_lines[0]))
            # 为每个游戏添加图片和信息
            for i, (game, cover_path) in enumerate(zip(results, cover_paths), 1):
                # 添加游戏信息
                game_info = (
                    f"{i}. 🆔 {game['id']}: {game['name']} "
                    f"(平台: {', '.join(game['platform'])})\n"
                    f"(语言: {', '.join(game['language'])})\n"
                )
                chain.append(Plain(game_info))
                # 添加封面图片（如果有）
                if cover_path and os.path.exists(cover_path):
                    chain.append(CompImage.fromFileSystem(cover_path))
                
            
            # 添加提示文本
            chain.append(Plain("\n📌 使用 '/下载gal <游戏ID>' 获取下载地址"))
            
            if len(results) > 5:
                node = Node(
                    uin=3974507586,
                    name="玖玖瑠",
                    content=chain
                )
                yield event.chain_result([node])
            else:
                # 发送消息
                yield event.chain_result(chain)
                
        except NoGameFound as e:
            yield event.plain_result(f"⚠️ {str(e)}")
        except APIError as e:
            logger.error(f"API请求错误: {str(e)}")
            yield event.plain_result("⚠️ 搜索失败，请稍后再试")
        except Exception as e:
            logger.error(f"未知错误: {type(e).__name__}: {str(e)}")
            yield event.plain_result("⚠️ 发生未知错误，请稍后再试")

    @filter.command("下载gal")
    async def download_galgame(self, event: AstrMessageEvent):
        """获取游戏下载地址（包含封面图片）"""
        cmd = event.message_str.split(maxsplit=1)
        if len(cmd) < 2:
            yield event.plain_result("⚠️ 参数错误，请输入游戏ID")
            return
            
        game_id = cmd[1]
        user_id = event.get_sender_id()
        
        try:
            # 验证ID格式
            if not game_id.isdigit():
                raise ValueError("游戏ID必须是数字")
                
            game_id = int(game_id)
            
            # 尝试从缓存获取游戏信息
            game_info = None
            if user_id in self.user_cache and game_id in self.user_cache[user_id]:
                game_info = self.user_cache[user_id][game_id]
            
            # 没有缓存则尝试直接获取
            if not game_info:
                # 先尝试从缓存中获取游戏名称
                game_name = "该游戏"
                for games in self.user_cache.values():
                    if game_id in games:
                        game_info = games[game_id]
                        game_name = game_info.get("name", "该游戏")
                        break
            
            # 获取游戏封面图片
            cover_image_path = None
            if game_info and game_info.get("banner"):
                try:
                    cover_image_path = await self.api.download_and_convert_image(game_info["banner"])
                except Exception as e:
                    logger.error(f"封面图处理失败: {str(e)}")
            
            yield event.plain_result(f"🔍 正在获取ID:{game_id}的下载资源...")
            downloads = await self.api.get_downloads(game_id)
            
            # 格式化结果
            game_name = game_info["name"] if game_info else f"ID:{game_id}"
            result = [
                f"🎮 游戏: {game_name} (ID: {game_id})",
                f"⬇️ 找到 {len(downloads)} 个下载资源:",
                self._format_downloads(downloads)
            ]
            
            # 构建消息链
            chain = []
            
            # 添加封面图片（如果有）
            if cover_image_path and os.path.exists(cover_image_path):
                chain.append(CompImage.fromFileSystem(cover_image_path))
            
            # 添加文本内容
            chain.append(Plain("\n".join(result)))
            
            # 发送消息
            yield event.chain_result(chain)
            
        except ValueError as e:
            yield event.plain_result(f"⚠️ {str(e)}")
        except DownloadNotFound as e:
            yield event.plain_result(f"⚠️ {str(e)}")
        except APIError as e:
            logger.error(f"API请求错误: {str(e)}")
            yield event.plain_result("⚠️ 下载查询失败，请稍后再试")
        except Exception as e:
            logger.error(f"未知错误: {type(e).__name__}: {str(e)}")
            yield event.plain_result("⚠️ 发生未知错误，请稍后再试")

    async def terminate(self):
        """插件终止时清理资源"""
        self.user_cache.clear()
        logger.info("TouchGal插件已终止，用户缓存已清空")
