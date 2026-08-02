import multiprocessing

bind = '0.0.0.0:5000'
workers = 2  # Pi 4 has 4 cores, 2 workers is reasonable
worker_class = 'sync'
timeout = 120
keepalive = 5
max_requests = 1000
max_requests_jitter = 50
accesslog = '-'
errorlog = '-'
loglevel = 'info'
