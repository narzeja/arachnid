''' Engine module
'''
import uvloop
import aiohttp
import asyncio
import logging
from time import time

from .response import Response
from .request import Request
from . import downloadermw
from . import spidermw
from . import resultmw
from . import defaultconfig
from . import utils
from .exceptions import IgnoreRequest
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


class Engine:
    def __init__(self, settings=None):
        self.queue = asyncio.Queue()
        self.spiders = {}
        self.settings = settings or defaultconfig
        self.seen_urls = set()

        self.running = False

        self.logger = self.getLogger()

    @classmethod
    def from_settings(cls, settings):
        obj = cls(settings)

        for spider in settings.spiders:
            try:
                module, _ = utils.load_module(spider['spider'])
            except ImportError as exc:
                obj.logger.error(exc)
                obj.logger.error('failed importing spider')
                raise
            else:
                spider_obj = utils.load_spider(module)
                registered = obj.register_spider(spider_obj)
                if not registered:
                    obj.logger.warning("Failed registering spider: %s", module)
                    continue

                for mw in spider.get('spider_middleware', []):
                    mw_obj = utils.load_module_obj(mw)
                    obj.spiders[spider_obj.name]['spidermwmanager']._add_middleware(mw_obj())

                for mw in spider.get('result_middleware', []):
                    mw_obj = utils.load_module_obj(mw)
                    obj.spiders[spider_obj.name]['resultmwmanager']._add_middleware(mw_obj())

        return obj

    def start(self):
        self.start_time = time()
        self.running = True
        self.loop = asyncio.get_event_loop()
        try:
            self.loop.run_until_complete(self.work())
        except KeyboardInterrupt:
            self.logger.error("User interrupted")
        self.loop.close()

    def stop(self):
        self.unregister_spiders()
        self.running = False
        self.stop_time = time()

    def getLogger(self):
        logger = logging.getLogger('Engine')
        if self.settings.log_level.lower() == 'debug':
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        # create console handler with a higher log level
        ch = logging.StreamHandler()
        # create formatter and add it to the handlers
        formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - [%(name)s] - %(message)s')
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        return logger

    def register_spider(self, spider):
        if spider.name not in self.spiders:
            logger = self.logger.getChild(spider.name)
            spider = spider(logger=logger)
            self.spiders[spider.name] = {
                'spider': spider,
                'downloadmwmanager': downloadermw.DownloaderMiddlewareManager(),
                'spidermwmanager': spidermw.SpiderMiddlewareManager(),
                'resultmwmanager': resultmw.ResultMiddlewareManager()
            }
        return spider

    def unregister_spiders(self):
        for spider_name, spider in self.spiders.items():
            self.close_spider(spider['spider'])

    def open_spider(self, spider):
        self.spiders[spider.name]['downloadmwmanager'].open_spider(spider)
        self.spiders[spider.name]['spidermwmanager'].open_spider(spider)
        self.spiders[spider.name]['resultmwmanager'].open_spider(spider)

    def close_spider(self, spider):
        self.spiders[spider.name]['downloadmwmanager'].close_spider(spider)
        self.spiders[spider.name]['spidermwmanager'].close_spider(spider)
        self.spiders[spider.name]['resultmwmanager'].close_spider(spider)
        spider.close_spider(reason='shutdown')

    async def fetch(self, task, logger, spider):
        self.seen_urls.add(task.url)

        response = await aiohttp.request('GET', task.url)
        content_type = response.headers['content-type']
        response.body = await response.read()

        logger.debug("Got a response: %s (code: %s)", response.url, response.status)
        response.close()
        response = Response(response.url,
                            response.status,
                            response.headers,
                            body=response.body,
                            request=task)
        return response

    async def distribute(self, task, logger):
        spider = task.callback.__self__
        callback_name = "%s.%s" % (spider.name,
                                   task.callback.__name__)
        logger.info("Got a task: %s (callback: %s)", task.url, callback_name)
        response = await self.spiders[spider.name]['downloadmwmanager'].download(self.fetch, task, logger.getChild('DownloadMW'), spider)

        if isinstance(response, Request):
            self.logger.debug("Got a request from downloader, putting in queue")
            self.queue.put_nowait(response)
            return
        if isinstance(response, IgnoreRequest):
            self.logger.debug("Downloader told us to ignore the request")
            return
        if isinstance(response, Exception):
            self.logger.error(response)
            return

        results_iter = await self.spiders[spider.name]['spidermwmanager'].scrape_response(task.callback, response, task, logger.getChild('SpiderMW'), spider)
        if isinstance(results_iter, Exception):
            self.logger.error(results_iter)
            return

        if not self.spiders[spider.name]['resultmwmanager'].methods['process_item']:
            self.logger.warning("You have no result pipeline, results will be discarded")

        self.logger.info("Found %d results (from: %s)", len(results_iter), callback_name)
        for result in results_iter:
            if isinstance(result, Request):
                self.queue.put_nowait(result)
            else:
                res = await self.spiders[spider.name]['resultmwmanager'].process_item(result, logger.getChild('ResultMW'), spider)

    async def consumer(self, executer_name):
        logger = self.logger.getChild(executer_name)
        if hasattr(self.settings, 'log_level'):
            if self.settings.log_level.lower() == 'debug':
                logger.setLevel(logging.DEBUG)

        while True:
            request = await self.queue.get()
            try:
                await self.distribute(request, logger)
            except (KeyboardInterrupt, MemoryError, SystemExit, asyncio.CancelledError) as e:
                raise
            except BaseException as e:
                logger.exception('Task distribution failed')
            finally:
                self.queue.task_done()


    async def work(self):
        # bootstrap and run executers
        for spider_name, spider in self.spiders.items():
            spider_inst = spider['spider']
            self.open_spider(spider_inst)
            for url in spider_inst.start_urls:
                await self.queue.put(Request(url, spider_inst.parse))

        num_executers = getattr(self.settings, 'engine', {'executers': 3}).get('executers', 3)

        self._workers = [self.loop.create_task(self.consumer('exec' + str(num)))
                         for num in range(num_executers)]

        self.logger.info("Started %d executers", len(self._workers))

        await self.queue.join()
        self.logger.info("Closing %d executers", len(self._workers))
        for w in self._workers:
            w.cancel()

        self.logger.info("Closing spiders")
        self.stop()
