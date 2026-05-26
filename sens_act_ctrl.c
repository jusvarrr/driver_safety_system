#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <gpiod.h>
#include <pigpio.h>
#include <sys/ioctl.h>
#include <linux/i2c-dev.h>
#include <math.h>
#include <time.h>

#define I2C_BUS                     "/dev/i2c-1"
#define M_PI                        3.14159265358979323846
#define METERS_PER_DEGREE           111132.0
#define BNO055_ADDR                 0x28
#define BNO055_OPR_MODE             0x3D
#define BNO055_UNIT_SEL             0x3B
#define MODE_NDOF                   0x0C

#define BNO055_LIA_X_LSB            0x28
#define BNO055_LIA_Y_LSB            0x2A
#define BNO055_EULER_H_LSB          0x1A

#define HUB_SOCKET_PATH             "/tmp/system_hub.sock"
#define BUFFER_SIZE                 128
#define GPIO_CHIP                   "/dev/gpiochip0"

#define BTN_CELL_CONN_TOGGLE        6
#define BTN_QUICK_LOCATION_SHARE    13
#define BTN_TRAVEL_LOG_DELETE       19
#define BTN_BUZZER_OFF              21

#define LED_MOBILE_CONN_STAT        17
#define LED_GNSS_STAT               27
#define LED_CAMERA_STAT             22

#define PWM_BUZZER                  12
#define DEBOUNCE_LIMIT              3

static int running = 1;
static int i2c_fd = -1;
static int cell_conn = 0, gnss_conn_lost = 0, cam_stat = 0, buzzer = 0, manual_correction = 0;

static struct gpiod_chip *chip = NULL;
static struct gpiod_line_request *input_req = NULL;
static struct gpiod_line_request *output_req = NULL;

static bool buzzer_turn_off = false;
static int hub_fd = -1;

static double pos_x = 0.0, pos_y = 0.0;
static double vel_x = 0.0, vel_y = 0.0;
static struct timespec last_time;

static double anchor_lat = 0.0;
static double anchor_lon = 0.0;
static double current_lat = 0.0;
static double current_lon = 0.0;

static double filtered_acc_x = 0.0;
static double filtered_acc_y = 0.0;
#define ALPHA 0.2

void send_hub_message(const char *topic, const char *json_payload);

void signal_handler(int sig) {
    (void)sig;
    running = 0;
}

void cleanup_gpio() {
    if (input_req) gpiod_line_request_release(input_req);
    if (output_req) gpiod_line_request_release(output_req);
    if (chip) gpiod_chip_close(chip);
}

void cleanup_socket() {
    if (hub_fd >= 0) {
        close(hub_fd);
        hub_fd = -1;
    }
}

void cleanup_all() {
    gpioPWM(PWM_BUZZER, 0);
    gpioTerminate();
    cleanup_socket();
    cleanup_gpio();
}

int setup_gpio() {
    chip = gpiod_chip_open(GPIO_CHIP);
    if (!chip) {
        perror("gpiod_chip_open");
        return -1;
    }

    struct gpiod_line_settings *in_settings = gpiod_line_settings_new();
    gpiod_line_settings_set_direction(in_settings, GPIOD_LINE_DIRECTION_INPUT);

    gpiod_line_settings_set_bias(in_settings, GPIOD_LINE_BIAS_PULL_DOWN);
    
    struct gpiod_line_config *in_cfg = gpiod_line_config_new();
    unsigned int in_offsets[] = {BTN_CELL_CONN_TOGGLE, BTN_QUICK_LOCATION_SHARE, BTN_TRAVEL_LOG_DELETE, BTN_BUZZER_OFF};
    gpiod_line_config_add_line_settings(in_cfg, in_offsets, 4, in_settings);

    input_req = gpiod_chip_request_lines(chip, NULL, in_cfg);
    gpiod_line_settings_free(in_settings);
    gpiod_line_config_free(in_cfg);

    struct gpiod_line_settings *out_settings = gpiod_line_settings_new();
    gpiod_line_settings_set_direction(out_settings, GPIOD_LINE_DIRECTION_OUTPUT);
    
    struct gpiod_line_config *out_cfg = gpiod_line_config_new();
    unsigned int out_offsets[] = {LED_MOBILE_CONN_STAT, LED_GNSS_STAT, LED_CAMERA_STAT};
    gpiod_line_config_add_line_settings(out_cfg, out_offsets, 3, out_settings);

    output_req = gpiod_chip_request_lines(chip, NULL, out_cfg);
    gpiod_line_settings_free(out_settings);
    gpiod_line_config_free(out_cfg);

    if (!input_req || !output_req) return -1;

    return 0;
}

int setup_bno055() {
    i2c_fd = open(I2C_BUS, O_RDWR);
    if (i2c_fd < 0) {
        perror("Failed to open I2C bus");
        return -1;
    }

    if (ioctl(i2c_fd, I2C_SLAVE, BNO055_ADDR) < 0) {
        perror("Failed to acquire bus access to BNO055");
        close(i2c_fd);
        return -1;
    }

    uint8_t config_buf[2] = {BNO055_OPR_MODE, 0x00};
    write(i2c_fd, config_buf, 2);
    usleep(30000);

    uint8_t unit_buf[2] = {BNO055_UNIT_SEL, 0x00};
    write(i2c_fd, unit_buf, 2);

    uint8_t ndof_buf[2] = {BNO055_OPR_MODE, MODE_NDOF};
    write(i2c_fd, ndof_buf, 2);
    usleep(30000); 

    clock_gettime(CLOCK_MONOTONIC, &last_time);
    printf("BNO055 IMU Initialized in NDOF Mode\n");
    return 0;
}

void update_dead_reckoning() {
    if (i2c_fd < 0) return;
    printf("[DR ERRORendtered!\n"); 

    uint8_t yaw_reg = BNO055_EULER_H_LSB;
    uint8_t yaw_buf[2];
    write(i2c_fd, &yaw_reg, 1);
    read(i2c_fd, yaw_buf, 2);

    int16_t yaw_raw = (yaw_buf[1] << 8) | yaw_buf[0];
    double yaw_deg = yaw_raw / 16.0; 
    double yaw_rad = yaw_deg * (M_PI / 180.0);

    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    double dt = (now.tv_sec - last_time.tv_sec) + (now.tv_nsec - last_time.tv_nsec) / 1e9;
    last_time = now;

    uint8_t reg = BNO055_LIA_X_LSB;
    uint8_t buffer[6];

    
    write(i2c_fd, &reg, 1);
    if (read(i2c_fd, buffer, 6) != 6) {
        printf("[DR ERROR] Failed to read BNO055 I2C data!\n");
        return;
    }

    int16_t x_raw = (buffer[1] << 8) | buffer[0];
    int16_t y_raw = (buffer[3] << 8) | buffer[2];

    double acc_x = x_raw / 100.0;
    double acc_y = y_raw / 100.0;

    if (fabs(acc_x) < 0.1) acc_x = 0; 
    if (fabs(acc_y) < 0.1) acc_y = 0;

    double global_acc_x = (acc_x * cos(yaw_rad)) - (acc_y * sin(yaw_rad));
    double global_acc_y = (acc_x * sin(yaw_rad)) + (acc_y * cos(yaw_rad));

    if (fabs(global_acc_x) < 0.15) global_acc_x = 0;
    if (fabs(global_acc_y) < 0.15) global_acc_y = 0;

    filtered_acc_x = (ALPHA * global_acc_x) + ((1.0 - ALPHA) * filtered_acc_x);
    filtered_acc_y = (ALPHA * global_acc_y) + ((1.0 - ALPHA) * filtered_acc_y);

    vel_x += filtered_acc_x * dt;
    vel_y += filtered_acc_y * dt;

    if (fabs(global_acc_x) < 0.01) vel_x = 0;
    if (fabs(global_acc_y) < 0.01) vel_y = 0;

    pos_x += vel_x * dt;
    pos_y += vel_y * dt;

    double lat_offset = pos_y / METERS_PER_DEGREE;
    double lon_offset = pos_x / (METERS_PER_DEGREE * cos(anchor_lat * M_PI / 180.0));

    current_lat = anchor_lat + lat_offset;
    current_lon = anchor_lon + lon_offset;

    char dr_payload[128];
    snprintf(dr_payload, sizeof(dr_payload), "{\"lat\": %f, \"lon\": %f}", current_lat, current_lon);
    send_hub_message("location/dr", dr_payload);
    printf("[DR DEBUG] Acc: %.2f, %.2f | Vel: %.2f, %.2f | Pos: %.2f, %.2f\n", 
            acc_x, acc_y, vel_x, vel_y, pos_x, pos_y);
}

void update_leds() {
    enum gpiod_line_value values[] = {
        cell_conn ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE,
        gnss_conn_lost ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE,
        cam_stat  ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE
    };
    if (gpiod_line_request_set_values(output_req, values) < 0) {
        perror("FAILED to set LED values");
    }
}

void toggle_buzzer() {
    static uint16_t current_pwm = 0;
    if (buzzer && current_pwm == 0 && !buzzer_turn_off) {
        gpioPWM(PWM_BUZZER, 128);
        current_pwm = 128;
        printf("Set buzzer ON\n");
    } else if ((buzzer_turn_off || !buzzer) && current_pwm != 0) {
        gpioPWM(PWM_BUZZER, 0);
        current_pwm = 0;
        buzzer = 0;
        buzzer_turn_off = false;
        printf("Set buzzer OFF\n");
    }
}

int connect_to_hub() {
    if ((hub_fd = socket(AF_UNIX, SOCK_STREAM, 0)) < 0) {
        perror("socket");
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, HUB_SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (connect(hub_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("connect to hub");
        close(hub_fd);
        hub_fd = -1;
        return -1;
    }

    int flags = fcntl(hub_fd, F_GETFL, 0);
    fcntl(hub_fd, F_SETFL, flags | O_NONBLOCK);

    printf("Connected to system hub on %s\n", HUB_SOCKET_PATH);
    return 0;
}

static char hub_rx_buffer[4096];
static int hub_rx_len = 0;

void process_hub_message(const char *msg) {
    char topic[64] = {0};
    
    // Safely extract Topic
    char *topic_start = strstr(msg, "\"topic\":");
    if (topic_start) {
        sscanf(topic_start, "\"topic\": \"%63[^\"]\"", topic);
    } else {
        return;
    }

    int val = 0;
    double lat_val = 0.0, lon_val = 0.0;

    // Safely extract variables regardless of JSON order or spacing
    char *state_ptr = strstr(msg, "\"state\":");
    if (state_ptr) sscanf(state_ptr, "\"state\": %d", &val);

    char *lat_ptr = strstr(msg, "\"lat\":");
    if (lat_ptr) sscanf(lat_ptr, "\"lat\": %lf", &lat_val);

    char *lon_ptr = strstr(msg, "\"lon\":");
    if (lon_ptr) sscanf(lon_ptr, "\"lon\": %lf", &lon_val);

    // --- Logic Routing ---
    if (strcmp(topic, "conn_stat/cell") == 0 && state_ptr) {
        cell_conn = !val;
    } 
    else if (strcmp(topic, "alert/buzzer") == 0 && state_ptr) {
        buzzer = val;
    }
    else if (strcmp(topic, "conn_stat/cam") == 0 && state_ptr) {
        cam_stat = val;
    }
    else if (strcmp(topic, "conn_stat/gnss") == 0 && state_ptr) {
        gnss_conn_lost = !val;
        if (gnss_conn_lost && lat_ptr && lon_ptr) {
            anchor_lat = lat_val; anchor_lon = lon_val;
            current_lat = lat_val; current_lon = lon_val;
        } else if (!gnss_conn_lost) {
            anchor_lat = lat_val; anchor_lon = lon_val;
            vel_x = 0.0; vel_y = 0.0; pos_x = 0.0; pos_y = 0.0;
            current_lat = lat_val; current_lon = lon_val;
            printf("[DR] GNSS recovered. Swapped to Dead Reckoning at anchor: %f, %f\n", anchor_lat, anchor_lon);
        }
    } 
    else if (strcmp(topic, "location/manual_correction") == 0) {
        if (lat_ptr && lon_ptr) {
            manual_correction = 1;
            anchor_lat = lat_val; anchor_lon = lon_val;
            current_lat = lat_val; current_lon = lon_val;
            vel_x = 0.0; vel_y = 0.0; pos_x = 0.0; pos_y = 0.0;
            printf("[DR] Manual Override! DR running from new anchor: %f, %f\n", anchor_lat, anchor_lon);
        }
    }
    else if (strcmp(topic, "location/manual_correction_off") == 0) {
        if (strstr(msg, "\"GPS\"")) {
            manual_correction = 0;
            printf("[DR] System commanded back to GPS mode. DR paused.\n");
        }
    }
}

void check_hub_messages() {
    if (hub_fd < 0) {
        connect_to_hub();
        return;
    }

    struct pollfd pfd;
    pfd.fd = hub_fd;
    pfd.events = POLLIN;

    if (poll(&pfd, 1, 10) > 0 && (pfd.revents & POLLIN)) {
        int bytes = read(hub_fd, hub_rx_buffer + hub_rx_len, sizeof(hub_rx_buffer) - hub_rx_len - 1);
        if (bytes <= 0) {
            printf("Hub disconnected\n");
            close(hub_fd);
            hub_fd = -1;
            hub_rx_len = 0;
            return;
        }
        
        hub_rx_len += bytes;
        hub_rx_buffer[hub_rx_len] = '\0';

        // Process line by line
        char *line_start = hub_rx_buffer;
        char *newline;
        while ((newline = strchr(line_start, '\n')) != NULL) {
            *newline = '\0';
            process_hub_message(line_start);
            line_start = newline + 1;
        }

        // Keep incomplete chunks in the buffer for the next read
        int remaining = hub_rx_len - (line_start - hub_rx_buffer);
        if (remaining > 0) {
            memmove(hub_rx_buffer, line_start, remaining);
        }
        hub_rx_len = remaining;
    }
}

void send_hub_message(const char *topic, const char *json_payload) {
    if (hub_fd < 0) return;

    char buffer[BUFFER_SIZE * 2];
    snprintf(buffer, sizeof(buffer), "{\"topic\": \"%s\", \"data\": %s}\n", topic, json_payload);

    int ret = write(hub_fd, buffer, strlen(buffer));
    if (ret <= 0) {
        perror("write to hub");
        close(hub_fd);
        hub_fd = -1;
    }
}

int buzzer_setup() {
    if (gpioInitialise() < 0) {
        printf("pigpio init failed\n");
        return -1;
    }

    gpioSetMode(PWM_BUZZER, PI_OUTPUT);
    gpioPWM(PWM_BUZZER, 0);
    return 0;
}

int main() {
    signal(SIGINT, signal_handler);

    if (setup_gpio() < 0) {
        cleanup_all();
        return 1;
    }

    if (connect_to_hub() < 0) {
        printf("Will try reconnecting to UDS..");
    }

    if (buzzer_setup() < 0) {
        cleanup_all();
        return 1;
    }

    if (setup_bno055() < 0) {
        printf("Proceeding without BNO055 Dead Reckoning support\n");
    }

    printf("GPIO started\n");
    uint8_t debounce_cnt[4] = {0};
    uint8_t sent[3] = {0};

    const char *topics[] = {"button/cell", "button/loc", "button/del"};
    const char *payloads[] = {"{\"state\": 1}", "{\"state\": 1}", "{\"state\": 1}"};

    struct timespec last_dr_time;
    clock_gettime(CLOCK_MONOTONIC, &last_dr_time);
    long dr_interval_ns = 3000000000L;

    while (running) {
        check_hub_messages();
        update_leds();
        toggle_buzzer();

        if (gnss_conn_lost || manual_correction) {
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);

            long elapsed_ns = (now.tv_sec - last_dr_time.tv_sec) * 1000000000L + 
                              (now.tv_nsec - last_dr_time.tv_nsec);

            if (elapsed_ns >= dr_interval_ns) {
                update_dead_reckoning();
                last_dr_time = now;
            }
        }   
        enum gpiod_line_value vals[4];
        gpiod_line_request_get_values(input_req, vals);

        for (int i = 0; i < 4; i++) {
            if (vals[i] == GPIOD_LINE_VALUE_ACTIVE) {
                debounce_cnt[i]++;
                if (debounce_cnt[i] > DEBOUNCE_LIMIT) {
                    if (i < 3) {
                        if (!sent[i]) {
                            printf("Sending: Topic=%s, Payload=%s\n", topics[i], payloads[i]);
                            send_hub_message(topics[i], payloads[i]);
                            sent[i] = 1;
                        }
                    } else if (i == 3) {
                        buzzer_turn_off = true;
                    }
                }
            } else {
                debounce_cnt[i] = 0;
                if (i < 3) sent[i] = 0;
            }
        }
        usleep(10000);
    }

    gpioPWM(PWM_BUZZER, 0);
    cleanup_all();
    return 0;
}