#!/usr/bin/env python
import importlib
import logging
import sys
import os
import inspect
import re
import web
import json
from manageInstance import ManageInstance
from ScalerSIP import ScalerSIP
from ScalerMedia import ScalerMedia

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

scalerConfigFile = os.environ.get("SCALER_CONFIG_FILE", "scaler.json")
cspName =  os.environ.get("CSP_NAME", "outscale")
cspConfigFile = os.environ.get("CSP_CONFIG_FILE", "sipmediagw_sample.json")
cspProfile = os.environ.get("CSP_PROFILE", "visio-dev")

scalerType = os.environ.get("SCALER_TYPE", "SIP")


def _scalerConfigPath():
    return "config/{}".format(scalerConfigFile)


def _cspConfigPath():
    return "{}/providers/{}/config/{}".format(
        os.path.dirname(os.path.abspath(__file__)), cspName, cspConfigFile
    )


def _importCspModule():
    """Import the provider module named by CSP_NAME."""
    providersDir = "{}/providers".format(os.path.dirname(os.path.abspath(__file__)))
    if providersDir not in sys.path:
        sys.path.append(providersDir)

    # Legacy providers are plain modules sitting inside their own directory, and
    # some of them import siblings by bare name, so that directory must be on the
    # path too. Packages such as openstackProvider are found via providersDir.
    legacyDir = "{}/{}".format(providersDir, cspName)
    if os.path.isdir(legacyDir) and legacyDir not in sys.path:
        sys.path.append(legacyDir)

    return importlib.import_module(cspName)


def _findCspClass(mod):
    """Return the single ManageInstance subclass exported by a provider module."""
    candidates = [
        cls
        for _, cls in inspect.getmembers(mod, inspect.isclass)
        if issubclass(cls, ManageInstance) and cls is not ManageInstance
    ]
    if not candidates:
        raise RuntimeError(
            "Provider '{}' exports no ManageInstance subclass".format(cspName)
        )
    if len(candidates) > 1:
        raise RuntimeError(
            "Provider '{}' exports several ManageInstance subclasses: {}".format(
                cspName, [cls.__name__ for cls in candidates]
            )
        )
    return candidates[0]


def _buildScaler():
    # Build the CSP provider + Scaler exactly once for the lifetime of the
    # process. Rebuilding them on every HTTP request was leaking ~3 sockets
    # and ~3.6 MB per call (one CSP connection per request, never closed),
    # which OOM-killed the process after ~12-18h of runtime.
    mod = _importCspModule()
    cspObj = _findCspClass(mod)
    logger.info("Loaded CSP provider %s from '%s'", cspObj.__name__, cspName)

    csp = cspObj(cspProfile)

    if scalerType.upper() == "SIP":
        scaler = ScalerSIP(csp)
    else:
        scaler = ScalerMedia(csp)

    scaler.configure(_scalerConfigPath())
    return scaler


_scaler = _buildScaler()


def authorize(func):
    def inner(*args, **kwargs):
        try:
            token = args[0].scaler.config['api_token']
        except (AttributeError, KeyError, TypeError, IndexError) as exc:
            logger.error("Cannot read the API token from the config: %s", exc)
            web.ctx.status = '500 Internal Server Error'
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

    def _reloadScalerConfig(self):
        if scalerType.upper() != "SIP":
            return
        try:
            self.scaler.configure(_scalerConfigPath())
        except Exception as exc:
            logger.warning("Failed to reload scaler config, keeping previous: %s", exc)

    def _configureCsp(self):
        """Reload the provider config and return the fresh per-action init data."""
        initData = {
            scalerType.lower(): {},
            # OpenStack (and any provider that scopes by name) uses this to own
            # only "<provider name>.<gw_name_prefix>..." servers.
            "gw_name_prefix": self.scaler.config.get("gw_name_prefix"),
        }
        self.scaler.csp.configureInstance(_cspConfigPath(), initData)
        return initData

    @authorize
    def GET(self, args=None):
        self._reloadScalerConfig()
        data = web.input()

        if 'auto' in data.keys():
            self._configureCsp()
            try:
                self.scaler.reconcile()
                self.scaler.cleanup()
                if self.scaler.scale() == 0:
                    web.ctx.status = '200 OK'
                    return json.dumps({"status": "success", "message": "The scaler iteration succeed"})
                web.ctx.status = '500 Internal Server Error'
                return json.dumps({"Error": "The scaler iteration did not complete"})
            except Exception as error:
                web.ctx.status = '500 Internal Server Error'
                return json.dumps({"Error": "The scaler iteration failed: {}".format(error)})

        if 'up' in data.keys():
            self._configureCsp()
            try:
                instRes = self.scaler.csp.createInstance(
                    '4', '4', name=self.scaler.config.get('gw_name_prefix')
                )
                web.ctx.status = '200 OK'
                return json.dumps({"status": "success", "instance": instRes})
            except Exception as error:
                web.ctx.status = '500 Internal Server Error'
                return json.dumps({"Error": "Instance creation failed: {}".format(error)})

        web.ctx.status = '400 Bad Request'
        return json.dumps({"Error": "Missing query parameter, expected 'auto' or 'up'"})



urls = ("/scale", "Scaling")

app = web.application(urls, globals())

if __name__ == "__main__":
    app.run()