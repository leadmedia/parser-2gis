from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import TYPE_CHECKING, Optional

from ...chrome import ChromeRemote
from ...common import wait_until_finished
from ...logger import logger
from ..utils import blocked_requests

if TYPE_CHECKING:
    from ...chrome import ChromeOptions
    from ...chrome.dom import DOMNode
    from ..options import ParserOptions


class ReviewsParser:
    """Parser for the list of reviews provided by 2GIS with the tab "Reviews".

    URL pattern for such cases: https://2gis.<domain>/<city_id>/firm/<firm_id>/reviews
    """

    def __init__(self, url: str,
                 chrome_options: ChromeOptions,
                 parser_options: ParserOptions) -> None:

        self._chrome_options = chrome_options
        self._options = parser_options
        self._url = url
        self.reviews_parser = ReviewsParser

        # "Catalog Item Document" response pattern.
        self._item_response_pattern = r'https://public-api\.reviews\.2gis.[^/]+/.*/reviews\?limit=12'

        # Open browser, start remote
        response_patterns = [self._item_response_pattern]
        self._chrome_remote = ChromeRemote(chrome_options=chrome_options,
                                           response_patterns=response_patterns)
        self._chrome_remote.start()

        # Add counter for 2GIS requsts
        self._add_xhr_counter()

        # Disable specific requests
        blocked_urls = blocked_requests(extended=chrome_options.disable_images)
        self._chrome_remote.add_blocked_requests(blocked_urls)

    def _add_xhr_counter(self) -> None:
        """Inject old-school wrapper around XMLHttpRequest,
        to keep track of all pending requests to 2GIS website."""
        xhr_script = r'''
            (function() {
                var oldOpen = XMLHttpRequest.prototype.open;
                XMLHttpRequest.prototype.open = function(method, url, async, user, pass) {
                    if (url.match(/^https?\:\/\/[^\/]*2gis\.[a-z]+/i)) {
                        if (window.openHTTPs == undefined) {
                            window.openHTTPs = 1;
                        } else {
                            window.openHTTPs++;
                        }
                        this.addEventListener("readystatechange", function() {
                            if (this.readyState == 4) {
                                window.openHTTPs--;
                            }
                        }, false);
                    }
                    oldOpen.call(this, method, url, async, user, pass);
                }
            })();
        '''
        self._chrome_remote.add_start_script(xhr_script)

    @staticmethod
    def url_pattern():
        """URL pattern for the parser."""
        return r'https?://2gis\.[^/]+/[^/]+/.*/tab/reviews'

    @wait_until_finished(timeout=5, throw_exception=False)
    def _get_links(self) -> list[DOMNode]:
        """Extracts specific DOM node links from current DOM snapshot."""

        def valid_link(node: DOMNode) -> bool:
            if node.local_name == 'a' and 'href' in node.attributes:
                link_match = re.match(r'/[^/]+/[^/]+/.*/tab/reviews$', node.attributes['href'])
                return bool(link_match)

            return False

        dom_tree = self._chrome_remote.get_document()
        return dom_tree.search(valid_link)

    @wait_until_finished(timeout=5, throw_exception=False)
    def _get_sidebar(self) -> list[DOMNode]:
        """Extracts specific DOM node links from current DOM snapshot."""

        def valid_link(node: DOMNode) -> bool:
            if node.attributes.get('class') == '' and node.local_name == 'div':
                return True
            return False

        dom_tree = self._chrome_remote.get_document()
        return dom_tree.search(valid_link)

    @wait_until_finished(timeout=120)
    def _wait_requests_finished(self) -> bool:
        """Wait for all pending requests."""
        return self._chrome_remote.execute_script('window.openHTTPs == 0')

    def parse(self) -> None or str:
        """Parse URL with organizations.

        Args:
            writer: Target file writer.
        """
        # Go URL
        self._chrome_remote.navigate(self._url, referer='https://google.com', timeout=120)

        # Document loaded, get its response
        responses = self._chrome_remote.get_responses(timeout=5)
        if not responses:
            logger.error('Ошибка получения ответа сервера.')
            return
        document_response = responses[0]

        # Handle 404
        assert document_response['mimeType'] == 'text/html'
        if document_response['status'] == 404:
            logger.warn('Сервер вернул сообщение "Точных совпадений нет / Не найдено".')

            if self._options.skip_404_response:
                return

        def load_reviews():
            if self._options.delay_between_clicks:
                self._chrome_remote.wait(self._options.delay_between_clicks / 1000)

            sidebar = self._get_sidebar()[0]
            self._chrome_remote.perform_scroll(sidebar)

            # Gather response and collect useful payload. Returns None after 3 seconds(means has no reviews to load)
            resp = self._chrome_remote.wait_responses(self._item_response_pattern)

            data = None

            # Get response body data
            if resp and resp['status'] >= 0:
                data = self._chrome_remote.get_response_body(resp, timeout=10) if resp else None

            return data

        def get_internal_reviews():
            html = str(self._chrome_remote.get_html())
            regex_json = r"var initialState = JSON\.parse\(\\'(.*?)\\'\);\s+window"
            json_match = re.search(regex_json, html, re.DOTALL)

            regex_reviews = r"(?<=\"objectSuggestions\":{},)(.*?),\"photo\":{},"
            reviews_match = re.search(regex_reviews, json_match[1], re.DOTALL)

            reviews_json = '{'+reviews_match[1]+'}'
            reviews_json = reviews_json.replace("\r\n", "")
            reviews_json = reviews_json.replace("\n", "")
            reviews_json = reviews_json.replace('\\\\', "\\")
            reviews_json = reviews_json.replace('\'', "\"")

            try:
                return json.loads(reviews_json)['review']
            except:
                # logger.info(reviews_json)
                return None

        # Wait all 2GIS requests get finished
        self._wait_requests_finished()

        reviews = []
        get_init_html = True
        while True:
            internal_reviews = None
            if get_init_html:
                internal_reviews = get_internal_reviews()
                get_init_html = False
            doc = load_reviews()

            if internal_reviews:
                for user_id in internal_reviews:
                    reviews.append(internal_reviews[user_id])
                    # logger.info(reviews)
                    # logger.info('Получен отзыв при загрузке страницы')

            if doc:
                for review in json.loads(doc)['reviews']:
                    reviews.append(review)

            if not doc:
                self._chrome_remote.stop()
                return reviews
