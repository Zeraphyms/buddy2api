import asyncio
import json
import unittest

from admin.metrics import MetricsMiddleware, RequestMetrics


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, status=200, chunks=None, stream=False, source="api", path="/v1/responses", crash=False):
        metrics = RequestMetrics()
        chunks = chunks if chunks is not None else [b'{"ok":true}']
        sent = []
        async def app(scope, receive, send):
            self.assertEqual(metrics.snapshot()['in_flight'], int(path == '/v1/responses'))
            await send({'type':'http.response.start','status':status,'headers':[(b'content-type', b'text/event-stream' if stream else b'application/json')]})
            for chunk in chunks:
                await send({'type':'http.response.body','body':chunk,'more_body':True})
            if crash:
                raise RuntimeError('interrupted')
            await send({'type':'http.response.body','body':b'','more_body':False})
        async def receive():
            return {'type':'http.request','body':b''}
        async def send(message):
            sent.append(message)
        scope={'type':'http','method':'POST','path':path}
        middleware=MetricsMiddleware(app,metrics,source)
        if crash:
            with self.assertRaises(RuntimeError): await middleware(scope,receive,send)
        else:
            await middleware(scope,receive,send)
        self.assertEqual(b''.join(m.get('body',b'') for m in sent), b''.join(chunks))
        return metrics.snapshot()

    async def test_success_and_http_error(self):
        s=await self.run_request(); self.assertEqual(s['completed'],1);self.assertEqual(s['success_rate'],100);self.assertEqual(s['in_flight'],0)
        s=await self.run_request(status=401);self.assertEqual(s['failed'],1);self.assertEqual(s['http_success_rate'],0)

    async def test_stream_error_with_200_is_not_success(self):
        s=await self.run_request(stream=True,chunks=[b'data: {"err',b'or":{"code":429}}\n\ndata: [DONE]\n\n'])
        self.assertEqual(s['http_success_rate'],100);self.assertEqual(s['success_rate'],0)
        self.assertEqual(s['recent'][0]['outcome'],'stream_error')

    async def test_complete_and_incomplete_stream(self):
        s=await self.run_request(stream=True,chunks=[b'data: {"type":"response.completed"}\n\n'])
        self.assertEqual(s['success_rate'],100)
        s=await self.run_request(stream=True,chunks=[b'data: {"delta":"text"}\n\n'])
        self.assertEqual(s['success_rate'],0);self.assertEqual(s['recent'][0]['outcome'],'interrupted')
        s=await self.run_request(stream=True,chunks=[b'data: {"type":"response.completed","response":{"status":"failed"}}\n'])
        self.assertEqual(s['failed'],1)

    async def test_exception_and_excluded_paths(self):
        s=await self.run_request(crash=True);self.assertEqual(s['failed'],1);self.assertEqual(s['in_flight'],0)
        s=await self.run_request(path='/admin/api/overview');self.assertEqual(s['completed'],0)
        s=await self.run_request(source='test');self.assertEqual(s['test_count'],1);self.assertEqual(s['api_count'],0)

    async def test_privacy_and_bounded_history(self):
        s=await self.run_request(chunks=[b'{"secret":"do-not-store"}'])
        self.assertNotIn('do-not-store',json.dumps(s))
        metrics=RequestMetrics()
        for _ in range(110):
            metrics.begin();metrics.finish('/v1/messages','api',200,True,10,'success')
        s=metrics.snapshot();self.assertEqual(len(s['recent']),100);self.assertEqual(s['completed'],110);self.assertEqual(s['avg_duration_ms'],10)

    async def test_client_disconnect(self):
        metrics=RequestMetrics()
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await receive()
        async def receive():return {'type':'http.disconnect'}
        async def send(m):pass
        await MetricsMiddleware(app,metrics)({'type':'http','method':'POST','path':'/v1/messages'},receive,send)
        s=metrics.snapshot();self.assertEqual(s['failed'],1);self.assertEqual(s['recent'][0]['outcome'],'interrupted')
    async def test_success_sink_learns_model_without_usage(self):
        """200 且无 usage 也要上报成功模型（学习「该账号能用此模型」）。

        回归：此前 learn 只挂在 usage_sink 上，免费档模型（credit=0）或
        适配层不回 usage 时，能调用却永远进不了账号可用列表。
        """
        learned = []
        metrics = RequestMetrics(success_sink=learned.append)
        async def app(scope, receive, send):
            await receive()  # 下游中间件总会先读请求体
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type', b'application/json')]})
            await send({'type':'http.response.body','body':b'{"ok":true}','more_body':False})
        body = json.dumps({"model":"deepseek-v4.1-flash"}).encode()
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        async def send(m): pass
        await MetricsMiddleware(app, metrics)({'type':'http','method':'POST','path':'/v1/chat/completions'},receive,send)
        self.assertEqual(learned, ['deepseek-v4.1-flash'])

    async def test_success_sink_uses_requested_model_when_response_omits_it(self):
        """响应不回 model 时用请求体里的模型名兜底。"""
        learned = []
        metrics = RequestMetrics(success_sink=learned.append)
        async def app(scope, receive, send):
            await receive()
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type', b'application/json')]})
            await send({'type':'http.response.body','body':b'{"ok":true}','more_body':False})
        body = json.dumps({"model":"some-alias"}).encode()
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        async def send(m): pass
        await MetricsMiddleware(app, metrics)({'type':'http','method':'POST','path':'/v1/chat/completions'},receive,send)
        self.assertEqual(learned, ['some-alias'])

    async def test_success_sink_not_called_on_failure(self):
        """失败/中断的请求不得记为「该账号能用此模型」。"""
        learned = []
        metrics = RequestMetrics(success_sink=learned.append)
        async def app(scope, receive, send):
            await receive()
            await send({'type':'http.response.start','status':429,
                        'headers':[(b'content-type', b'application/json')]})
            await send({'type':'http.response.body','body':b'{"error":true}','more_body':False})
        body = json.dumps({"model":"m"}).encode()
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        async def send(m): pass
        await MetricsMiddleware(app, metrics)({'type':'http','method':'POST','path':'/v1/chat/completions'},receive,send)
        self.assertEqual(learned, [])

    async def test_request_body_not_stored_in_snapshot(self):
        """请求体只用于取 model，绝不进入快照。"""
        metrics = RequestMetrics()
        async def app(scope, receive, send):
            await receive()
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type', b'application/json')]})
            await send({'type':'http.response.body','body':b'{"ok":true}','more_body':False})
        body = json.dumps({"model":"m","messages":[{"role":"user","content":"do-not-store-me"}]}).encode()
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        async def send(m): pass
        await MetricsMiddleware(app, metrics)({'type':'http','method':'POST','path':'/v1/chat/completions'},receive,send)
        self.assertNotIn('do-not-store-me', json.dumps(metrics.snapshot()))


if __name__=='__main__':unittest.main()
