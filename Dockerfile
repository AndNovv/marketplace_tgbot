# Use the official Python image from the Docker Hub
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt requirements.txt

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Set environment variable for the Telegram bot token
ENV TELEGRAM_BOT_TOKEN=6564190447:AAE5UDZ8PIesH2WYne1TV9zphs71Fu5nezY

# Define the command to run the application
CMD ["python", "priceCheckerBot.py"]