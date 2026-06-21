# -*- coding: utf-8 -*-
"""
实现搜书吧论坛登入和发布空间动态
"""
import os
import re
import sys
from copy import copy

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
import time
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

ch = logging.StreamHandler(stream=sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)


REQUEST_TIMEOUT = 30


def _normalise_encoding(response: requests.Response) -> None:
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding


def _extract_meta_refresh_url(response: requests.Response) -> str | None:
    soup = BeautifulSoup(response.text, 'html.parser')
    meta_tag = soup.find(
        'meta',
        attrs={'http-equiv': lambda value: value and value.lower() == 'refresh'}
    )
    if not meta_tag:
        return None

    content = meta_tag.get('content', '')
    match = re.search(r'url\s*=\s*(.+)$', content, flags=re.I)
    if not match:
        return None

    redirect_url = match.group(1).strip().strip('\'"')
    return urljoin(response.url, redirect_url)


def get_refresh_url(url: str, session: requests.Session | None = None) -> str | None:
    session = session or requests.Session()
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    _normalise_encoding(response)
    redirect_url = _extract_meta_refresh_url(response)
    if redirect_url:
        logger.info(f'Redirecting to: {redirect_url}')
        return redirect_url

    if response.status_code >= 400:
        response.raise_for_status()
    return None


def _extract_forum_url(response: requests.Response) -> str | None:
    soup = BeautifulSoup(response.text, 'html.parser')
    for link in soup.find_all('a', href=True):
        text = link.get_text(strip=True)
        if text == "搜书吧":
            return urljoin(response.url, link['href'])
    return None


def get_url(url: str, session: requests.Session | None = None) -> str | None:
    session = session or requests.Session()
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    _normalise_encoding(resp)
    return _extract_forum_url(resp)


def resolve_forum_url(hostname: str, max_hops: int = 8) -> str:
    session = requests.Session()
    url = hostname if hostname.startswith(('http://', 'https://')) else f'http://{hostname}'

    for _ in range(max_hops):
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        _normalise_encoding(response)

        redirect_url = _extract_meta_refresh_url(response)
        if redirect_url:
            logger.info(f'Redirecting to: {redirect_url}')
            url = redirect_url
            continue

        forum_url = _extract_forum_url(response)
        if forum_url:
            return forum_url

        if response.status_code >= 400:
            response.raise_for_status()

        raise ValueError('Could not find 搜书吧 forum link on release page.')

    raise ValueError(f'Too many redirects while resolving {hostname}.')


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f'Missing required environment variable: {name}')
    return value


def extract_discuz_message(text: str) -> str:
    soup = BeautifulSoup(text, 'html.parser')
    message = soup.find(id=re.compile(r'^(returnmessage_|messagetext)'))
    if message:
        return message.get_text(' ', strip=True)
    return soup.get_text(' ', strip=True)[:200]


class SouShuBaClient:

    def __init__(self, hostname: str, username: str, password: str, questionid: str = '0', answer: str = None,
                 proxies: dict | None = None):
        self.session: requests.Session = requests.Session()
        self.hostname = hostname
        self.username = username
        self.password = password
        self.questionid = questionid
        self.answer = answer
        self._common_headers = {
            "Host": f"{ hostname }",
            "Connection": "keep-alive",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,cn;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        self.proxies = proxies

    def _get(self, url: str) -> requests.Response:
        resp = self.session.get(url, proxies=self.proxies, timeout=REQUEST_TIMEOUT)
        _normalise_encoding(resp)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict, headers: dict) -> requests.Response:
        resp = self.session.post(
            url,
            proxies=self.proxies,
            data=data,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        _normalise_encoding(resp)
        resp.raise_for_status()
        return resp

    def _has_auth_cookie(self) -> bool:
        return any(name.endswith('_auth') for name in self.session.cookies.keys())

    @staticmethod
    def _has_logged_in_uid(text: str) -> bool:
        match = re.search(r"discuz_uid\s*=\s*'(\d+)'", text)
        return bool(match and match.group(1) != '0')

    def login_form(self):
        resp = self._get(f'https://{self.hostname}/member.php?mod=logging&action=login')
        soup = BeautifulSoup(resp.text, 'html.parser')
        form = soup.find('form', attrs={'name': 'login'}) or soup.find('form', id=re.compile(r'^loginform_'))
        if not form:
            raise ValueError('Could not find Discuz login form.')

        action = form.get('action')
        if not action:
            raise ValueError('Discuz login form does not include an action URL.')

        payload = {}
        for field in form.find_all(['input', 'select']):
            name = field.get('name')
            if name:
                payload[name] = field.get('value', '')

        payload.update({
            'username': self.username,
            'password': self.password,
            'questionid': self.questionid,
            'answer': self.answer or '',
            'cookietime': payload.get('cookietime') or '2592000',
        })
        return urljoin(resp.url, action), payload

    def login(self):
        """Login with username and password"""
        login_url, payload = self.login_form()
        headers = copy(self._common_headers)
        headers["origin"] = f'https://{self.hostname}'
        headers["referer"] = f'https://{self.hostname}/'

        resp = self._post(login_url, data=payload, headers=headers)
        if self._has_auth_cookie() or self._has_logged_in_uid(resp.text):
            logger.info(f'Welcome {self.username}!')
            return

        home_resp = self._get(f'https://{self.hostname}/home.php')
        if self._has_auth_cookie() or self._has_logged_in_uid(home_resp.text):
            logger.info(f'Welcome {self.username}!')
            return

        message = extract_discuz_message(resp.text)
        raise ValueError(f'Verify failed: {message}')

    def credit(self):
        credit_url = f"https://{self.hostname}/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu"
        credit_rst = self._get(credit_url).text

        # 解析 XML，提取 CDATA
        root = ET.fromstring(str(credit_rst))
        cdata_content = root.text or ''

        # 使用 BeautifulSoup 解析 CDATA 内容
        cdata_soup = BeautifulSoup(cdata_content, features="lxml")
        hcredit_2 = cdata_soup.find("span", id="hcredit_2")
        if not hcredit_2:
            raise ValueError(f'Could not parse credit: {extract_discuz_message(cdata_content or credit_rst)}')

        return hcredit_2.get_text(strip=True)

    def space_form_hash(self):
        rst = self._get(f'https://{self.hostname}/home.php').text
        match = re.search(r'<input type="hidden" name="formhash" value="(.+?)"\s*/?>', rst)
        if not match:
            match = re.search(r"formhash\s*=\s*'(.+?)'", rst)
        if not match:
            raise ValueError('Could not find formhash on home page.')
        return match.group(1)

    def space(self):
        formhash = self.space_form_hash()
        space_url = f"https://{self.hostname}/home.php?mod=spacecp&ac=doing&handlekey=doing&inajax=1"

        headers = copy(self._common_headers)
        headers["origin"] = f'https://{self.hostname}'
        headers["referer"] = f'https://{self.hostname}/home.php'

        for x in range(5):
            payload = {
                "message": "开心赚银币 {0} 次".format(x + 1).encode("GBK"),
                "addsubmit": "true",
                "spacenote": "true",
                "referer": "home.php",
                "formhash": formhash
            }
            resp = self._post(space_url, data=payload, headers=headers)
            if re.search("操作成功", resp.text):
                logger.info(f'{self.username} post {x + 1}nd successfully!')
                time.sleep(120)
            elif "您需要先登录" in resp.text:
                raise ValueError('Session expired or login failed before posting space message.')
            else:
                logger.warning(f'{self.username} post {x + 1}nd failed: {extract_discuz_message(resp.text)}')


if __name__ == '__main__':
    try:
        url = resolve_forum_url(require_env('SOUSHUBA_HOSTNAME'))
        logger.info(f'{url}')
        client = SouShuBaClient(urlparse(url).hostname,
                                require_env('SOUSHUBA_USERNAME'),
                                require_env('SOUSHUBA_PASSWORD'))
        client.login()
        client.space()
        credit = client.credit()
        logger.info(f'{client.username} have {credit} coins!')
    except Exception as e:
        logger.error(e)
        sys.exit(1)
