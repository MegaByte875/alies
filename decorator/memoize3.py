__author__ = 'qiaolei'
#Alternate memoize as dict subclass


class memoize(dict):

    def __init__(self, func):
        self.func = func

    def __call__(self, *args):
        return self[args]

    def __missing__(self, key):
        """
        d[key]
        Return the item of d with key key. Raises a KeyError if key is not in the map.
        New in version 2.5: If a subclass of dict defines a method __missing__(),
        if the key key is not present, the d[key] operation calls that method with the key key as argument.
        The d[key] operation then returns or raises whatever is returned or raised by the __missing__(key) call
        if the key is not present. No other operations or methods invoke __missing__().
        If __missing__() is not defined, KeyError is raised. __missing__() must be a method;
        it cannot be an instance variable. For an example, see 'collections.defaultdict'.
        """
        result = self[key] = self.func(*key)
        return result

@memoize
def test(a, b):
    return a * b

print test(2, 4)

print test('hi', 3)

print test
