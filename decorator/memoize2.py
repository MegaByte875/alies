__author__ = 'qiaolei'
#Alternate memoize as nested functions

import functools

def memoize(obj):

    cache = obj.cache = {}

    @functools.wraps(obj)
    def memoizer(*args, **kwargs):
        key = str(args) + str(kwargs)
        if key not in cache:
            cache[key] = obj(*args, **kwargs)
        return cache[key]

    return memoizer

@memoize
def fibonacci(n):
    if n in (0, 1):
        return n
    else:
        return fibonacci(n-1) + fibonacci(n-2)

print fibonacci(12)