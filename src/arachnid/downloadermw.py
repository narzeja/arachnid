# -*- coding: utf-8 -*-

"""
arachnid.downloadermw

This module implements the Downloader Middleware Manager

"""

import asyncio

from . import middleware
from .request import Request
from .response import Response


def _isiterable(possible_iterator):
    return hasattr(possible_iterator, '__iter__')


class DownloaderMiddlewareManager(middleware.MiddlewareManager):
    """
    Responsibilities:

    * Execute all middlewares that operate on outgoing Requests.
    * Download the target URL
    * Execute all middlewares that operate on Incoming Responses.

.. method:: process_request(requests, spider)

    This method is called for each request that goes through the download middleware.
    Run middleware on outgoing Request, return either ``None``, Response, Request, or raise IgnoreRequest

    * If Response, return response and execute no more middleware
    * If Request, return request and execute no more middleware. Request should be put back in task-queue for later execution
    * If ``None``, continue running download_func on the now maybe manipulated Request
    * If raise IgnoreRequest, the process_exception handler is invoked, and no further response/request processing is done


.. method:: process_response(request, response, spider)

    This method is called for each response that is returned from the Downloader, after having processed the request.
    Run middleware on incoming Response, return either :class:`~arachnid.response.Response` or :class:`~arachnid.request.Request`.

    * If :class:`~arachnid.response.Response`, continue running middlewares
    * If :class:`~arachnid.request.Request`, abort running more middlewares and return the Request. Request should be put back in task-queue for later execution
    * If only Reponses are returned from middlewares, it will be passed on to the scraper function (handled by SpiderMiddlewareManager)


.. method:: process_exception(request, exception, spider)

    This method is called when exceptions occur in :meth:`process_request` and :meth:`process_response`.

"""
    name = 'downloader middleware'

    def _add_middleware(self, mw):
        super()._add_middleware(mw)
        if hasattr(mw, 'process_request'):
            self.methods['process_request'].append(mw.process_request)
        if hasattr(mw, 'process_response'):
            self.methods['process_response'].insert(0, mw.process_response)
        if hasattr(mw, 'process_exception'):
            self.methods['process_exception'].insert(0, mw.process_exception)

    async def download(self, download_func, request, logger, spider):
        async def process_request(request):
            for method in self.methods['process_request']:
                response = method(request=request, spider=spider)
                assert response is None or isinstance(response, (Response, Request)), \
                    'Middleware {}.process_request must return None, Response or Request, got {}'.format(
                        method.__class__.__name__, response.__class__.__name__)

                if response:
                    return response

            resp = await download_func(request, logger, spider)
            return resp

        async def process_response(response):
            assert response is not None, 'Received None in process_response'
            if isinstance(response, Request):
                return response

            for method in self.methods['process_response']:
                response = method(request=request, response=response, spider=spider)
                assert response is None or isinstance(response, (Response, Request)), \
                    'Middleware {}.process_response must return Response or Request, got {}'.format(
                        method.__class__.__name__, response.__class__.__name__)

                if isinstance(response, Request):
                    return response
            return response

        async def process_exception(_failure):
            exception = _failure
            for method in self.methods['process_exception']:
                result = method(request=request, exception=exception, spider=spider)
                assert result is None or _isiterable(result), \
                    'Middleware {} must returns None, or an iterable object, got {}'.format(
                        method.__class__.__name__, type(result))
                if result is not None:
                    return result
            return _failure

        try:
            resp = await process_request(request)
            resp = await process_response(resp)
        except Exception as exc:
            return await process_exception(exc)
        else:
            return resp
