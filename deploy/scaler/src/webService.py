#!/usr/bin/env python
import importlib
import logging
import sys
import os
import datetime as dt
import inspect
import re
import web
import json
from ScalerSIP  import ScalerSIP
from ScalerMedia import ScalerMedia

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

scalerConfigFile = os.environ.get("SCALER_CONFIG_FILE", "scaler.json")
cspName =  os.environ.get("CSP_NAME", "outscale")
cspConfigFile = os.environ.get("CSP_CONFIG_FILE", "sipmediagw_sample.json")
cspProfile = os.environ.get("CSP_PROFILE", "visio-dev")

scalerType = os.environ.get("SCALER_TYPE", "SIP")


def _buildScaler():
    # Build the CSP provider + Scaler exactly once for the lifetime of the
    # process. Rebuilding them on every HTTP request was leaking ~3 sockets
    # and ~3.6 MB per call (one CSP connection per request, never closed),
    # which OOM-killed the process after ~12-18h of runtime.
    sys.path.append("{}/providers/{}".format(os.path.dirname(os.path.abspath(__file__)), cspName))
    modName = cspName
    print("CSP mod name: "+modName, flush=True)
    mod = importlib.import_module(modName)
    isClassMember = lambda member: inspect.isclass(member) and member.__module__ == modName
    cspObj = inspect.getmembers(mod, isClassMember)[0][1]

    csp = cspObj(cspProfile)

    if scalerType.upper() == "SIP":
        scaler = ScalerSIP(csp)
    else:
        scaler = ScalerMedia(csp)

    scaler.configure("config/{}".format(scalerConfigFile))
    return scaler


_scaler = _buildScaler()


def authorize(func):
    def inner(*args, **kwargs):
        try:
            token = args[0].scaler.config['api_token']
        except:
            return json.dumps({'Error': 'internal error'})
        auth = web.ctx.env.get('HTTP_AUTHORIZATION')
        authReq = False
        if auth is None:
            authReq = True
        else:
            auth = re.sub('^Bearer ', '', auth)
            if auth != token:
                authReq = True
        if not authReq:
            return func(*args, **kwargs)
        else:
            web.header('WWW-Authenticate', 'Bearer error="invalid_token"')
            web.ctx.status = '401 Unauthorized'
            return json.dumps({'Error': 'authorization error'})
    return inner

class Scaling:
    def __init__(self) -> None:
        self.scaler = _scaler

    @authorize
    def GET(self, args=None):
        data = web.input()
        if 'auto' in data.keys():
            initData = { scalerType.lower() : {}}
            self.scaler.csp.configureInstance("{}/providers/{}/config/{}".format(
                os.path.dirname(os.path.abspath(__file__)), cspName, cspConfigFile), initData)
            try:
                self.scaler.cleanup()
                if self.scaler.scale() == 0:
                    web.ctx.status = '200 OK'
                    return json.dumps({"status": "success", "message": "The scaler iteration succeed"})
            except Exception as error:
                return "The scaler iteration failed: {}".format(error)
        if 'up' in data.keys():
            initData [scalerType.lower()] = {}
            self.scaler.csp.configureInstance("{}/config/{}".format(cspName, cspConfigFile), initData)
            try:
                instRes = self.scaler.csp.createInstance('4','4', name='mediagw')
                web.ctx.status = '200 OK'
                return json.dumps({"status": "success", "instance": instRes})
            except Exception as error:
                web.ctx.status = '500 Internal Server Error'
                return json.dumps({"Error": "Instance creation failed: {}".format(error)})



urls = ("/scale", "Scaling")

app = web.application(urls, globals())

if __name__ == "__main__":
    app.run()