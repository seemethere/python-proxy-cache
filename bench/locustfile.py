from locust import HttpUser, between, task


class PyPIUser(HttpUser):
    wait_time = between(0.01, 0.05)

    @task(3)
    def simple_json_hit(self):
        self.client.get("/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}, name="/simple/{project}/ json")

    @task(1)
    def simple_html_hit(self):
        self.client.get("/simple/requests/", headers={"Accept": "text/html"}, name="/simple/{project}/ html")

    @task(1)
    def simple_index(self):
        self.client.get("/simple/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}, name="/simple/")

# run: locust -f bench/locustfile.py --host http://localhost:8080  (via nginx) or :8000 direct
