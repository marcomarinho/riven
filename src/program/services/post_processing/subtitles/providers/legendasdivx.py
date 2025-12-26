"""
LegendasDivx provider for Riven.
"""

import io
import re
import time
import zipfile
from urllib.parse import parse_qs, quote, urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from program.settings import settings_manager
from program.settings.models import SubtitleConfig
from .base import SubtitleItem, SubtitleProvider


class LegendasDivxProvider(SubtitleProvider):
    """
    LegendasDivx.pt provider implementation.
    """

    def __init__(self):
        self.settings = settings_manager.settings.post_processing.subtitle.providers.legendasdivx
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://www.legendasdivx.pt',
            'Origin': 'https://www.legendasdivx.pt'
        })
        self.base_url = "https://www.legendasdivx.pt"
        self.login_url = f"{self.base_url}/forum/ucp.php?mode=login"
        self.login_url = f"{self.base_url}/forum/ucp.php?mode=login"
        # Note: download_link is better constructed from the 'd_op=getit' pattern safely
        
    @property
    def name(self) -> str:
        return "legendasdivx"

    def initialize(self) -> bool:
        """Login to LegendasDivx."""
        if not self.settings.username or not self.settings.password:
            logger.warning("LegendasDivx credentials not configured")
            return False

        try:
            # 1. Get login page to fetch SID
            logger.debug("LegendasDivx: Fetching login page")
            resp = self.session.get(self.login_url)
            resp.raise_for_status()
            
            soup = BeautifulSoup(resp.content, "lxml")
            sid_input = soup.find("input", {"name": "sid"})
            sid = sid_input.get("value") if sid_input else None
            
            # 2. Post login
            data = {
                "username": self.settings.username,
                "password": self.settings.password,
                "login": "Entrar",
            }
            if sid:
                data["sid"] = sid
            
            # Collect hidden inputs
            for inp in soup.find_all("input", type="hidden"):
                 if inp.get("name") and inp.get("name") not in data:
                     data[inp.get("name")] = inp.get("value")

            time.sleep(1) # Be nice
            
            logger.debug("LegendasDivx: Posting login")
            post_resp = self.session.post(self.login_url, data=data)
            post_resp.raise_for_status()
            
            # Check if logged in (usually check for logout link or PHPSESSID)
            if "ucp.php?mode=logout" in post_resp.text:
                logger.debug("LegendasDivx: Logged in successfully")
                return True
            else:
                logger.error("LegendasDivx: Login failed (check credentials or captcha)")
                return False

        except Exception as e:
            logger.error(f"LegendasDivx initialization error: {e}")
            return False

    def search_subtitles(
        self,
        imdb_id: str,
        video_hash: str | None = None,
        file_size: int | None = None,
        filename: str | None = None,
        search_tags: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        language: str = "en",
    ) -> list[SubtitleItem]:
        
        # Ensure logged in
        if not self.session.cookies.get("PHPSESSID"):
            if not self.initialize():
                return []

        results = list[SubtitleItem]()
        try:
            # Determine search term (IMDB ID or Name)
            # Determine search term (IMDB ID or Name)
            search_term = ""
            using_imdb = False
            if imdb_id:
                search_term = imdb_id.lstrip("tt")
                using_imdb = True
            elif filename:
                # Try to extract name from filename as fallback
                clean_name = filename.replace(".", " ").replace("_", " ")
                
                if season is not None and episode is not None:
                     # Look for SxxExx pattern
                     match = re.search(r"(.+?)\s+S(\d+)", clean_name, re.IGNORECASE)
                     if match:
                         search_term = match.group(1).strip()
                     else:
                         search_term = clean_name
                else:
                     # Movie: Look for Year
                     match = re.search(r"(.+?)\s+(\d{4})", clean_name)
                     if match:
                         search_term = match.group(1).strip()
                     else:
                         search_term = clean_name
            
            # Prepare search parameters

            d_op = "search"
            op = "_jz00"
            season_str = ""
            episode_str = ""

            if season is not None and episode is not None:
                if using_imdb:
                    d_op = "jz_00"
                    op = ""
                    query_param = "&faz=pesquisa_episodio"
                    season_str = str(season)
                    episode_str = str(episode)
                else:
                    querytext = '"{}" S{:02d}E{:02d}'.format(search_term, season, episode)
                    query_param = quote(querytext.lower())
            else:
                query_param = quote(search_term)

            # Check if Portuguese is requested
            extra_params = ""
            subtitle_settings = settings_manager.settings.post_processing.subtitle
            if any(lang in ["pt", "por", "pob"] for lang in subtitle_settings.languages):
                 extra_params += "&idioma=28"

            # Construct URL
            # Note: query param already contains encoded term + extra flags if needed
            # We must be careful with 'imdb' param. If searching by filename, omit it.
            
            base_url = f"{self.base_url}/modules.php?name=Downloads&file=jz&d_op={d_op}&op={op}&query={query_param}&temporada={season_str}&episodio={episode_str}"
            
            if using_imdb:
                 base_url += f"&imdb={search_term}"
            
            url = f"{base_url}{extra_params}"

            logger.debug(f"LegendasDivx: Searching {url}")
            resp = self.session.get(url)
            resp.raise_for_status()
            
            soup = BeautifulSoup(resp.content, "lxml")
            
            # Parse results
            sub_boxes = soup.find_all("div", class_="sub_box")
            
            for box in sub_boxes:
                # Check language
                # <th class=brd_down>Idioma:</th> <td class=brd_down><img ... src=.../brazil.png></td>
                lang_img = box.find("img", src=re.compile(r"(brazil|portugal)\.png"))
                sub_lang = "und"
                if lang_img:
                    src = lang_img.get("src", "").lower()
                    if "brazil" in src:
                        sub_lang = "pob" # pt-BR
                    elif "portugal" in src:
                        sub_lang = "por" # pt-PT
                
                # Filter by language
                if sub_lang != language:
                    continue
                
                # Extract info
                desc_td = box.find("td", class_="td_desc")
                desc = desc_td.get_text(strip=True) if desc_td else ""
                
                # Hits
                hits = 0
                hits_node = box.find(string=re.compile("Hits:"))
                if hits_node:
                    hits_td = hits_node.find_next("td")
                    if hits_td:
                        try:
                            hits = int(hits_td.get_text(strip=True))
                        except ValueError:
                            pass
                        
                # Link
                footer = box.find("div", class_="sub_footer")
                dl_link = None
                if footer:
                    a_tag = footer.find("a", class_="sub_download")
                    if a_tag:
                        dl_link = a_tag["href"]
                
                if not dl_link:
                    continue
                
                # Extract LID for cleaner ID
                # Link format: modules.php?name=Downloads&d_op=viewdownloaddetails&lid=42699
                lid_match = re.search(r"lid=(\d+)", dl_link)
                sub_id = lid_match.group(1) if lid_match else f"ld_{hash(dl_link)}"

                # Score
                score = 0
                matched_by = "unknown"
                
                if search_tags: 
                    tags = search_tags.split(",")
                    matches = sum(1 for tag in tags if tag.lower() in desc.lower())
                    if matches > 0:
                        score += 100 * matches
                        matched_by = "tag"
                
                if filename and filename.lower() in desc.lower():
                     score += 50
                     matched_by = "filename"
                
                # Default score if we found it via IMDB search
                score += 100

                results.append(SubtitleItem(
                    id=sub_id,
                    language=sub_lang,
                    filename=f"{sub_id}.srt",
                    download_count=hits,
                    rating=0.0,
                    matched_by=matched_by,
                    movie_hash=None,
                    movie_name=desc.split("\n")[0][:50],
                    provider=self.name,
                    score=score
                ))

        except Exception as e:
            logger.error(f"LegendasDivx search failed: {e}")
            
        return results

    def download_subtitle(self, subtitle_info: SubtitleItem) -> str | None:
        if not self.session.cookies.get("PHPSESSID"):
            if not self.initialize():
                return None
            
        lid = subtitle_info.id
        dl_url = f"{self.base_url}/modules.php?name=Downloads&d_op=getit&lid={lid}"
        
        try:
            logger.debug(f"LegendasDivx: Downloading {dl_url}")
            resp = self.session.get(dl_url)
            resp.raise_for_status()
            
            content = resp.content
            
            # Check for archive headers
            if content.startswith(b"PK"):
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as zf:
                        for name in zf.namelist():
                            if name.lower().endswith((".srt", ".sub", ".vtt")):
                                return self._decode_content(zf.read(name))
                except Exception as e:
                    logger.error(f"LegendasDivx ZIP extraction failed: {e}")
            
            elif content.startswith(b"Rar!"):
                try:
                    import rarfile
                    with rarfile.RarFile(io.BytesIO(content)) as rf:
                         for name in rf.namelist():
                             if name.lower().endswith((".srt", ".sub", ".vtt")):
                                 return self._decode_content(rf.read(name))
                except ImportError:
                    logger.warning("LegendasDivx: RAR archive found but rarfile not installed, skipping")
                except Exception as e:
                    logger.error(f"LegendasDivx RAR extraction failed: {e}")
                    
            else:
                 # Assume raw text
                 return self._decode_content(content)
                 
        except Exception as e:
            logger.error(f"LegendasDivx download failed: {e}")
            
        return None

    def _decode_content(self, content_bytes: bytes) -> str | None:
        encodings = ["utf-8", "iso-8859-1", "windows-1252", "cp1252"]
        for enc in encodings:
            try:
                decoded = content_bytes.decode(enc)
                if len(decoded.strip()) > 0:
                     return decoded
            except UnicodeDecodeError:
                continue
        # Fallback
        return content_bytes.decode("utf-8", errors="replace")
