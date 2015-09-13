#!/usr/bin/python

import os
import sys

print "The child will write text to a pipe and "
print "the parent will read the text written by child..."

# file descriptors r, w for reading and writing
r, w = os.pipe()

processid = os.fork()
if processid:
    # This is the parent process
    # Closes file descriptor w
    os.close(w)
    r = os.fdopen(r)
    print "Parent reading"
    str1 = r.read()
    print "text =", str1
    os.waitpid(processid, 0) # wait for child process terminate
    sys.exit(0)
else:
    # This is the child process
    os.close(r)
    w = os.fdopen(w, 'w')
    print "Child writing"
    w.write("Text written by child...")
    w.close()
    print "Child closing"
    sys.exit(0)
