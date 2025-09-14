import time
_state = {"started": int(time.time()*1000)}
def set_k(k, v): _state[k] = v
def get(): return _state