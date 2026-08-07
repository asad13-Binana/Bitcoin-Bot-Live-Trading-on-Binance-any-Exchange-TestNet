from __future__ import annotations
import secrets, time, threading

class CallbackStore:
    def __init__(self, ttl=120):
        self.ttl=ttl; self.items={}; self.used={}; self.lock=threading.Lock()

    def _cleanup(self, now):
        self.items={k:v for k,v in self.items.items() if v[0]>=now}
        self.used={k:expiry for k,expiry in self.used.items() if expiry>=now}

    def issue(self, action, args=None):
        with self.lock:
            now=time.time(); self._cleanup(now)
            token=secrets.token_urlsafe(16)
            self.items[token]=(now+self.ttl,action,args or {})
            return token

    def consume(self, token):
        with self.lock:
            now=time.time()
            if token in self.used:
                if self.used[token] >= now:
                    return None,'duplicate callback'
                self.used.pop(token,None)
            item=self.items.pop(token,None)
            self._cleanup(now)
            if not item: return None,'unknown callback'
            if item[0]<now: return None,'expired callback'
            self.used[token]=item[0]
            return {'action':item[1],'args':item[2]},'ok'

    def cancel(self, token):
        with self.lock:
            now=time.time(); self._cleanup(now)
            item=self.items.pop(token,None)
            if not item:
                return False,'unknown callback'
            if item[0]<now:
                return False,'expired callback'
            self.used[token]=item[0]
            return True,'canceled'
