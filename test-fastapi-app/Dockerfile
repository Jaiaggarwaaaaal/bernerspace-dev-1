# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the project files into the container
COPY pyproject.toml README.md ./

# Install any needed packages specified in pyproject.toml
RUN pip install --no-cache-dir .

# Copy the rest of the application source code
COPY main.py .

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
