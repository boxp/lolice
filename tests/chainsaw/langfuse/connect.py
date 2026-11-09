import socket,sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 4).close()
except TimeoutError:
    sys.exit(3)
