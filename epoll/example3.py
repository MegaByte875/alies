import socket
import select

EOL1 = b'\n\n'
EOL2 = b'\n\r\n'
response = b'HTTP/1.0 200 OK\r\nDate: Mon, 1 Jan 1996 01:01:01 GMT\r\n'
response += b'Content-Type: text/plain\r\nContent-Length: 13\r\n\r\n'
response += b'Hello, world!'

serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
serversocket.bind(('0.0.0.0', 8080))
serversocket.listen(1)
serversocket.setblocking(0)

epoll = select.epoll()
epoll.register(serversocket.fileno(), select.EPOLLIN)

try:
    connections = {}; requests = {}; responses = {}
    while True:
        events = epoll.poll(1)
        for fileno, event in events:
            if fileno == serversocket.fileno():
                connection, address = serversocket.accept()
                connection.setblocking(0)
                epoll.register(connection.fileno(), select.EPOLLIN)
                connections[connection.fileno()] = connection
                requests[connection.fileno()] = b''
                responses[connection.fileno()] = response
            elif event & select.EPOLLIN:
                requests[fileno] += connections[fileno].recv(1024)
                if EOL1 in requests[fileno] or EOL2 in requests[fileno]:
                    epoll.modify(fileno, select.EPOLLOUT)
                    print('-'*40 + '\n' + requests[fileno].decode()[:-2])
            elif event & select.EPOLLOUT:
                byteswritten = connections[fileno].send(responses[fileno])
                responses[fileno] = responses[fileno][byteswritten:]
                if len(responses[fileno]) == 0:
                    epoll.modify(fileno, 0)
                    connections[fileno].shutdown(socket.SHUT_RDWR)
            elif event & select.EPOLLHUP:
                epoll.unregister(fileno)
                connections[fileno].close()
                del connections[fileno]
finally:
    epoll.unregister(serversocket.fileno())
    epoll.close()
    serversocket.close()

# 第1行：select模块包含epoll功能。
# 第13行：因为socket默认是阻塞的，所以需要使用非阻塞（异步）模式。
# 第15行：创建一个epoll对象。
# 第16行：在服务端socket上面注册对读event的关注。一个读event随时会触发服务端socket去接收一个socket连接。
# 第19行：字典connections映射文件描述符（整数）到其相应的网络连接对象。
# 第21行：查询epoll对象，看是否有任何关注的event被触发。参数“1”表示，我们会等待1秒来看是否有event发生。如果有任何我们感兴趣的event发生在这次查询之前，这个查询就会带着这些event的列表立即返回。
# 第22行：event作为一个序列（fileno，event code）的元组返回。fileno是文件描述符的代名词，始终是一个整数。
# 第23行：如果一个读event在服务端sockt发生，就会有一个新的socket连接可能被创建。
# 第25行：设置新的socket为非阻塞模式。
# 第26行：为新的socket注册对读（EPOLLIN）event的关注。
# 第31行：如果发生一个读event，就读取从客户端发送过来的新数据。
# 第33行：一旦完成请求已收到，就注销对读event的关注，注册对写（EPOLLOUT）event的关注。写event发生的时候，会回复数据给客户端。
# 第34行：打印完整的请求，证明虽然与客户端的通信是交错进行的，但数据可以作为一个整体来组装和处理。
# 第35行：如果一个写event在一个客户端socket上面发生，它会接受新的数据以便发送到客户端。
# 第36-38行：每次发送一部分响应数据，直到完整的响应数据都已经发送给操作系统等待传输给客户端。
# 第39行：一旦完整的响应数据发送完成，就不再关注读或者写event。
# 第40行：如果一个连接显式关闭，那么socket shutdown是可选的。本示例程序这样使用，是为了让客户端首先关闭。shutdown调用会通知客户端socket没有更多的数据应该被发送或接收，并会让功能正常的客户端关闭自己的socket连接。
# 第41行：HUP（挂起）event表明客户端socket已经断开（即关闭），所以服务端也需要关闭。没有必要注册对HUP event的关注。在socket上面，它们总是会被epoll对象注册。
# 第42行：注销对此socket连接的关注。
# 第43行：关闭socket连接。
# 第18-45行：使用try-catch，因为该示例程序最有可能被KeyboardInterrupt异常中断。
# 第46-48行：打开的socket连接不需要关闭，因为Python会在程序结束的时候关闭。这里显式关闭是一个好的代码习惯。
	