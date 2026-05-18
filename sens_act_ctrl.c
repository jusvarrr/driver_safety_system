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
#define BNO055_ADDR                 0x28
#define BNO055_OPR_MODE             0x3D
#define BNO055_UNIT_SEL             0x3B
#define MODE_NDOF                   0x0C

#define BNO055_EULER_H_LSB          0x1A
#define BNO055_LIA_X_LSB            0x28
#define BNO055_LIA_Y_LSB            0x2A

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
static int cell_conn = 0, gnss_conn = 0, cam_stat = 0, buzzer = 0;

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

    uint8_t reg;
    uint8_t data[6];
    
}

void update_leds() {
    enum gpiod_line_value values[] = {
        cell_conn ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE,
        gnss_conn ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE,
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

void process_message(const char *msg) {
    char topic[64] = {0};
    int val = 0;
    double lat_val = 0.0, lon_val = 0.0;

    int parsed = sscanf(msg, "{\"topic\": \"%63[^\"]\", \"data\": {\"state\": %d}}", topic, &val);
    if (parsed != 2) {
        parsed = sscanf(msg, "{\"topic\":\"%63[^\"]\",\"data\":{\"state\":%d}}", topic, &val);
    }

    if (parsed == 2) {
        printf("Debug State Parsed: Topic=%s, Val=%d\n", topic, val);
        
        if (strcmp(topic, "conn_stat/cell") == 0) cell_conn = val;
        else if (strcmp(topic, "alert/buzzer") == 0) buzzer = val;
        return;
    }

    int parsed_gps = sscanf(msg, "{\"topic\": \"%63[^\"]\", \"data\": {\"state\":%d, \"lon\": %lf, \"lat\": %lf}}", topic, &val, &lon_val, &lat_val);
    if (parsed_gps != 4) {
        parsed_gps = sscanf(msg, "{\"topic\":\"%63[^\"]\",\"data\":{\"state\":%d,\"lon\":%lf,\"lat\":%lf}}", topic, &val, &lon_val, &lat_val);
    }

    if (parsed_gps == 4) {
        if (strcmp(topic, "conn_stat/gnss") == 0) {
            gnss_conn = val;
            if (gnss_conn == 1) {
                anchor_lat = lat_val;
                anchor_lon = lon_val;
                current_lat = lat_val;
                current_lon = lon_val;
            } else {
                vel_x = 0.0; vel_y = 0.0;
                pos_x = 0.0; pos_y = 0.0;
                printf("No Gnss! Starting calculate at %f, %f with DR.\n", anchor_lat, anchor_lon);
            }
        }
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

void check_hub_messages() {
    if (hub_fd < 0) {
        connect_to_hub();
        return;
    }

    struct pollfd pfd;
    pfd.fd = hub_fd;
    pfd.events = POLLIN;

    int ret = poll(&pfd, 1, 10);
    if (ret < 0) {
        if (errno != EINTR) perror("poll hub");
        return;
    }

    if (ret > 0 && (pfd.revents & POLLIN)) {
        char buffer[BUFFER_SIZE * 2];
        int bytes = read(hub_fd, buffer, sizeof(buffer) - 1);
        if (bytes <= 0) {
            printf("Hub disconnected\n");
            close(hub_fd);
            hub_fd = -1;
            return;
        }
        buffer[bytes] = '\0';
        process_message(buffer);
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
        cleanup_all();
        return 1;
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

    while (running) {
        check_hub_messages();
        update_leds();
        toggle_buzzer();

        if (gnss_conn == 0)
            update_dead_reckoning();        
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