CC = gcc
CFLAGS = -Wall -O2
LIBS = -lpigpio -lrt -lpthread -lgpiod -lm

TARGET = sensor_ctrl
SRC = sens_act_ctrl.c

.PHONY: all clean install-pigpio

all: install-pigpio $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(SRC) -o $(TARGET) $(LIBS)
	@echo "Done compiling code for sensor and actuator control: ./$(TARGET)"

install-pigpio:
	@if [ ! -f /usr/local/include/pigpio.h ]; then \
		echo "pigpio not found. Installing from GitHub..."; \
		git clone https://github.com/joan2937/pigpio /tmp/pigpio; \
		cd /tmp/pigpio && make && sudo make install; \
		rm -rf /tmp/pigpio; \
	else \
		echo "pigpio already installed."; \
	fi

clean:
	rm -f $(TARGET)
	@echo "Cleanup complete."