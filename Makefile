all: migrate-with-fixtures

package-dev:
	python setup.py sdist bdist_wheel
	twine upload --repository testpypi dist/*
	rm -rf build dist

package:
	python setup.py sdist bdist_wheel
	twine upload --repository pypi dist/*
	rm -rf build dist

migrate:
	autoreduce-webapp-manage migrate

migrate-with-fixtures: migrate
	autoreduce-webapp-manage loaddata super_user_fixture status_fixture software_fixture pr_test

selenium:
	sudo docker kill selenium && docker rm selenium || echo "Selenium container isn't already running, just starting it."
	sudo docker run --network host --name selenium --rm -d -v /dev/shm:/dev/shm selenium/standalone-chrome:4.0.0-beta-3-prerelease-20210422

mysql-test:
	sudo docker kill mysql-ar || echo "Selenium container isn't already running, just starting it."
	sudo docker run --name mysql-ar -e MYSQL_ROOT_PASSWORD=password -e MYSQL_DATABASE=test_autoreduce --rm -p3306:3306 -d mysql:latest
	until nc -w 1 127.0.0.1 3306; do sleep 1; done
