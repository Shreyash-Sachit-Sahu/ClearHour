FROM public.ecr.aws/lambda/python:3.13

# LightGBM needs the OpenMP runtime; the Python 3.13 base image is Amazon Linux 2023 minimal (dnf = microdnf)
RUN dnf install -y libgomp && dnf clean all

COPY functions/forecast/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src/clearhour ${LAMBDA_TASK_ROOT}/clearhour

CMD ["clearhour.handlers.forecast.handler"]
