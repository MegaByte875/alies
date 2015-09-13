#Alternate memoize as nested functions
#Here's a memoizing function that works on functions, methods, or classes, and exposes the cache publicly.
import functools


# note that this decorator ignores **kwargs
def memoize(obj):

    cache = obj.cache = {}

    @functools.wraps(obj)
    def memoizer(*args, **kwargs):
        if args not in cache:
            cache[args] = obj(*args, **kwargs)
        return cache[args]
    return memoizer

@memoize
def fibonacci(n):
    if n in (0, 1):
        return n
    else:
        return fibonacci(n-1) + fibonacci(n-2)

print fibonacci(12)