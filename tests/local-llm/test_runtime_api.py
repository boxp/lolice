import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('worker', Path(__file__).resolve().parents[2] / 'argoproj/local-llm/runtime_api_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class TransportTest(unittest.TestCase):
    def request(self, path=None, method='GET', body=None):
        return dict(method=method, path=path or next(iter(worker.TARGETS)), body=body, expires=time.time()+30)

    def patch(self, pointer='/spec', value=None):
        return [dict(op='test', path='/metadata/uid', value='uid'),
                dict(op='test', path='/metadata/resourceVersion', value='123'),
                dict(op='test', path=pointer, value={} if pointer == '/spec' else 'closed:BOXP-349-test'),
                dict(op='replace', path=pointer, value={'replicas': 1} if value is None else value)]

    def test_exact_boundary(self):
        worker.validate(self.request())
        worker.validate(self.request(method='PATCH', body=self.patch()))
        for path in ['/api/v1/secrets', '/apis/apps/v1/namespaces/local-llm/deployments/llama-server']:
            with self.assertRaises(ValueError): worker.validate(self.request(path))
        for value in [{'replicas': 2}, {'replicas': True}, {'replicas': 1, 'template': {}}]:
            with self.assertRaises(ValueError): worker.validate(self.request(method='PATCH', body=self.patch(value=value)))
        with self.assertRaises(ValueError): worker.validate(self.request(method='PATCH', body=self.patch()[2:]))

    def test_gate_and_expiry(self):
        path = list(worker.TARGETS)[2]
        worker.validate(self.request(path, 'PATCH', self.patch('/data/state', 'verify:BOXP-349-test')))
        with self.assertRaises(ValueError): worker.validate(self.request(path, 'PATCH', self.patch('/data/state', 'open:../../bad')))
        request=self.request();request['expires']=time.time()-1
        with self.assertRaises(ValueError): worker.validate(request)

    def test_crash_never_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['attempts','responses','requests']: (root/name).mkdir()
            path=root/'requests'/('a'*32+'.json');path.write_text(json.dumps(self.request()))
            called=[]
            def uncertain(request): called.append(request); raise TimeoutError()
            worker.process(root,path,uncertain)
            worker.process(root,path,uncertain)
            self.assertEqual(len(called),1)
            self.assertEqual(json.loads((root/'responses'/path.name).read_text()),{'status':'unknown'})
            # Crash after durable intent but before response: no second API call.
            path2=root/'requests'/('b'*32+'.json');path2.write_text(json.dumps(self.request()))
            worker.write_new(root/'attempts'/path2.name,{'started':time.time()})
            worker.process(root,path2,uncertain)
            self.assertEqual(len(called),1)
            self.assertEqual(json.loads((root/'responses'/path2.name).read_text()),{'status':'unknown'})

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'response.json'
            worker.write_new(path,{'status':200})
            with self.assertRaises(FileExistsError): worker.write_new(path,{'status':500})
            self.assertEqual(json.loads(path.read_text()),{'status':200})


if __name__=='__main__': unittest.main()
