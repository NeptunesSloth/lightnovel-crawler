import socket


def free_port(host: str = "127.0.0.1", desired_port: int = 0) -> int:
    """
    Returns desired_port if it is free, otherwise returns a random free port.

    `host` must be the interface the caller is going to listen on. Asking about
    one address and then binding another is not a detail: Windows lets a socket
    take 127.0.0.1:P while another process already holds 0.0.0.0:P, so probing
    loopback for a server that binds the wildcard reports a port as free that
    the server cannot actually have. Linux rejects that second bind, which is
    why the mismatch looks harmless until it ships.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, desired_port))
        except OSError:
            s.bind((host, 0))
        return s.getsockname()[1]
