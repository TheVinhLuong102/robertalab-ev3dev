
from PIL import Image, ImageFont
import dbus    # only for waitForConnection() bluetooth
import glob    # only for stopAllMotors()
import logging
import math
import os
import threading  # only for ledOn() animations
import time

# ignore failure to make this testable outside of the target platform
try:
    # this has not be release to debian
    # https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=787850
    import bluetooth
    from bluetooth import BluetoothSocket
except ImportError:
    pass

try:
    from ev3dev import auto as ev3dev
except ImportError:
    from .test import Ev3dev as ev3dev

logger = logging.getLogger('roberta.ev3')


def clamp(v, mi, ma):
    return mi if v < mi else ma if v > ma else v


class Hal(object):
    # class global, so that the front-end can cleanup on forced termination
    # popen objects
    cmds = []
    # led blinker
    led_blink_thread = None
    led_blink_running = False

    GYRO_MODES = {
        'angle': 'GYRO-ANG',
        'rate': 'GYRO-RATE',
    }

    LED_COLORS = {
        'green': ev3dev.Leds.GREEN + ev3dev.Leds.GREEN,
        'red': ev3dev.Leds.RED + ev3dev.Leds.RED,
        'orange': ev3dev.Leds.ORANGE + ev3dev.Leds.ORANGE,
        'black': ev3dev.Leds.BLACK + ev3dev.Leds.BLACK,
    }

    LED_ALL = ev3dev.Leds.LEFT + ev3dev.Leds.RIGHT

    def __init__(self, brickConfiguration):
        self.cfg = brickConfiguration
        dir = os.path.dirname(__file__)
        # char size: 6 x 12 -> num-chars: 29.666667 x 10.666667
        self.font_s = ImageFont.load(os.path.join(dir, 'ter-u12n_unicode.pil'))
        # char size: 10 x 18 -> num-chars: 17.800000 x 7.111111
        # self.font_s = ImageFont.load(os.path.join(dir, 'ter-u18n_unicode.pil'))
        self.lcd = ev3dev.Screen()
        self.led = ev3dev.Leds
        self.keys = ev3dev.Button()
        self.sound = ev3dev.Sound
        (self.font_w, self.font_h) = self.lcd.draw.textsize('X', font=self.font_s)
        # logger.info('char size: %d x %d -> num-chars: %f x %f',
        #     self.font_w, self.font_h, 178 / self.font_w, 128 / self.font_h)
        self.timers = {}
        self.sys_bus = None
        self.bt_server = None
        self.bt_connections = []
        self.lang = 'de'

    # factory methods
    @staticmethod
    # TODO(ensonic): 'regulated' is unused, it is passed to the motor-functions
    # directly, consider making all params after port 'kwargs'
    def makeLargeMotor(port, regulated, direction):
        try:
            m = ev3dev.LargeMotor(port)
            if direction == 'backward':
                m.polarity = 'inversed'
            else:
                m.polarity = 'normal'
        except (AttributeError, OSError):
            logger.info('no large motor connected to port [%s]', port)
            logger.exception("HW Config error")
            m = None
        return m

    @staticmethod
    def makeMediumMotor(port, regulated, direction):
        try:
            m = ev3dev.MediumMotor(port)
            if direction == 'backward':
                m.polarity = 'inversed'
            else:
                m.polarity = 'normal'
        except (AttributeError, OSError):
            logger.info('no medium motor connected to port [%s]', port)
            logger.exception("HW Config error")
            m = None
        return m

    @staticmethod
    def makeOtherConsumer(port, regulated, direction):
        try:
            lp = ev3dev.LegoPort(port)
            lp.mode = 'dc-motor'
            # https://github.com/ev3dev/ev3dev-lang-python/issues/234
            # some time is needed to set the permissions for the new attributes
            time.sleep(0.5)
            m = ev3dev.DcMotor(address=port)
        except (AttributeError, OSError):
            logger.info('no other consumer connected to port [%s]', port)
            logger.exception("HW Config error")
            m = None
        return m

    @staticmethod
    def makeColorSensor(port):
        try:
            s = ev3dev.ColorSensor(port)
        except (AttributeError, OSError):
            logger.info('no color sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeGyroSensor(port):
        try:
            s = ev3dev.GyroSensor(port)
        except (AttributeError, OSError):
            logger.info('no gyro sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeI2cSensor(port):
        try:
            s = ev3dev.I2cSensor(port)
        except (AttributeError, OSError):
            logger.info('no i2c sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeInfraredSensor(port):
        try:
            s = ev3dev.InfraredSensor(port)
        except (AttributeError, OSError):
            logger.info('no infrared sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeLightSensor(port):
        try:
            p = ev3dev.LegoPort(port)
            p.set_device = 'lego-nxt-light'
            s = ev3dev.LightSensor(port)
        except (AttributeError, OSError):
            logger.info('no light sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeSoundSensor(port):
        try:
            p = ev3dev.LegoPort(port)
            p.set_device = 'lego-nxt-sound'
            s = ev3dev.SoundSensor(port)
        except (AttributeError, OSError):
            logger.info('no sound sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeTouchSensor(port):
        try:
            s = ev3dev.TouchSensor(port)
        except (AttributeError, OSError):
            logger.info('no touch sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeUltrasonicSensor(port):
        try:
            s = ev3dev.UltrasonicSensor(port)
        except (AttributeError, OSError):
            logger.info('no ultrasonic sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeCompassSensor(port):
        try:
            s = ev3dev.Sensor(address=port, driver_name='ht-nxt-compass')
        except (AttributeError, OSError):
            logger.info('no compass sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeIRSeekerSensor(port):
        try:
            s = ev3dev.Sensor(address=port, driver_name='ht-nxt-ir-seek-v2')
        except (AttributeError, OSError):
            logger.info('no ir seeker v2 sensor connected to port [%s]', port)
            s = None
        return s

    @staticmethod
    def makeHTColorSensorV2(port):
        try:
            s = ev3dev.Sensor(address=port, driver_name='ht-nxt-color-v2')
        except (AttributeError, OSError):
            logger.info('no hitechnic color sensor v2 connected to port [%s]', port)
            s = None
        return s

    # state
    def resetState(self):
        self.clearDisplay()
        self.stopAllMotors()
        self.resetAllOutputs()
        self.resetLED()
        logger.debug("terminate %d commands", len(Hal.cmds))
        for cmd in Hal.cmds:
            if cmd:
                logger.debug("terminate command: %s", str(cmd))
                cmd.terminate()
                cmd.wait()  # avoid zombie processes
        Hal.cmds = []

    # control
    def waitFor(self, ms):
        time.sleep(ms / 1000.0)

    def busyWait(self):
        """Used as interruptable busy wait."""
        time.sleep(0.0)

    def waitCmd(self, cmd):
        """Wait for a command to finish."""
        Hal.cmds.append(cmd)
        # we're not using cmd.wait() since that is not interruptable
        while cmd.poll() is None:
            self.busyWait()
        Hal.cmds.remove(cmd)

    # lcd
    def drawText(self, msg, x, y, font=None):
        font = font or self.font_s
        self.lcd.draw.text((x * self.font_w, y * self.font_h), msg, font=font)
        self.lcd.update()

    def drawPicture(self, picture, x, y):
        # logger.info('len(picture) = %d', len(picture))
        size = (178, 128)
        # One image is supposed to be 178*128/8 = 2848 bytes
        # string data is in utf-16 format and padding with extra 0 bytes
        data = bytes(picture, 'utf-16')[::2]
        pixels = Image.frombytes('1', size, data, 'raw', '1;IR', 0, 1)
        self.lcd.image.paste(pixels, (x, y))
        self.lcd.update()

    def clearDisplay(self):
        self.lcd.clear()
        self.lcd.update()

    # led

    def ledStopAnim(self):
        if Hal.led_blink_running:
            Hal.led_blink_running = False
            Hal.led_blink_thread.join()
            Hal.led_blink_thread = None

    def ledOn(self, color, mode):
        def ledAnim(anim):
            while Hal.led_blink_running:
                for step in anim:
                    self.led.set_color(Hal.LED_ALL, step[1])
                    time.sleep(step[0])
                    if not Hal.led_blink_running:
                        break

        self.ledStopAnim()
        # color: green, red, orange - LED.COLOR.{RED,GREEN,AMBER}
        # mode: on, flash, double_flash
        on = Hal.LED_COLORS[color]
        off = Hal.LED_COLORS['black']
        if mode == 'on':
            self.led.set_color(Hal.LED_ALL, on)
        elif mode == 'flash':
            Hal.led_blink_thread = threading.Thread(
                target=ledAnim, args=([(0.5, on), (0.5, off)],))
            Hal.led_blink_running = True
            Hal.led_blink_thread.start()
        elif mode == 'double_flash':
            Hal.led_blink_thread = threading.Thread(
                target=ledAnim, args=([(0.15, on), (0.15, off), (0.15, on), (0.55, off)],))
            Hal.led_blink_running = True
            Hal.led_blink_thread.start()

    def ledOff(self):
        self.led.all_off()
        self.ledStopAnim()

    def resetLED(self):
        self.ledOff()

    # key
    def isKeyPressed(self, key):
        if key in ['any', '*']:
            return self.keys.any()
        else:
            # remap some keys
            key_aliases = {
                'escape':  'backspace',
                'back': 'backspace',
            }
            if key in key_aliases:
                key = key_aliases[key]
            return key in self.keys.buttons_pressed

    def isKeyPressedAndReleased(self, key):
        return False

    # tones
    def playTone(self, frequency, duration):
        # this is already handled by the sound api (via beep cmd)
        # frequency = frequency if frequency >= 100 else 0
        self.waitCmd(self.sound.tone(frequency, duration))

    def playFile(self, systemSound):
        # systemSound is a enum for preset beeps:
        # http://www.lejos.org/ev3/docs/lejos/hardware/Audio.html#systemSound-int-
        # http://sf.net/p/lejos/ev3/code/ci/master/tree/ev3classes/src/lejos/remote/nxt/RemoteNXTAudio.java#l20
        C2 = 523
        if systemSound == 0:
            self.playTone(600, 200)
        elif systemSound == 1:
            self.sound.tone([(600, 150, 50), (600, 150, 50)]).wait()
        elif systemSound == 2:  # C major arpeggio
            self.sound.tone([(C2 * i / 4, 50, 50) for i in range(4, 7)]).wait()
        elif systemSound == 3:
            self.sound.tone([(C2 * i / 4, 50, 50) for i in range(7, 4, -1)]).wait()
        elif systemSound == 4:
            self.playTone(100, 500)

    def setVolume(self, volume):
        self.sound.set_volume(volume)

    def getVolume(self):
        return self.sound.get_volume()

    def setLanguage(self, lang):
        # lang: 2digit ISO_639-1 code
        self.lang = lang

    def sayText(self, text, speed=30, pitch=50):
        # a: amplitude, 0..200, def=100
        # p: pitch, 0..99, def=50
        # s: speed, 80..450, def=175
        opts = '-a 200 -p %d -s %d -v %s' % (
            int(clamp(pitch, 0, 100) * 0.99),  # use range 0 - 99
            int(clamp(speed, 0, 100) * 2.5 + 100),  # use range 100 - 350
            self.lang + "+f1")  # female voice
        self.waitCmd(self.sound.speak(text, espeak_opts=opts))

    # actors
    # http://www.ev3dev.org/docs/drivers/tacho-motor-class/
    def scaleSpeed(self, m, speed_pct):
        return int(speed_pct * m.max_speed / 100.0)

    def rotateRegulatedMotor(self, port, speed_pct, mode, value):
        # mode: degree, rotations, distance
        m = self.cfg['actors'][port]
        speed = self.scaleSpeed(m, clamp(speed_pct, -100, 100))
        if mode == 'degree':
            m.run_to_rel_pos(position_sp=value, speed_sp=speed)
            while m.state and 'stalled' not in m.state:
                self.busyWait()
        elif mode == 'rotations':
            value *= m.count_per_rot
            m.run_to_rel_pos(position_sp=int(value), speed_sp=speed)
            while m.state and 'stalled' not in m.state:
                self.busyWait()

    def rotateUnregulatedMotor(self, port, speed_pct, mode, value):
        speed_pct = clamp(speed_pct, -100, 100)
        m = self.cfg['actors'][port]
        if mode == 'rotations':
            value *= m.count_per_rot
        if speed_pct >= 0:
            value = m.position + value
            m.run_direct(duty_cycle_sp=int(speed_pct))
            while m.position < value and 'stalled' not in m.state:
                self.busyWait()
        else:
            value = m.position - value
            m.run_direct(duty_cycle_sp=int(speed_pct))
            while m.position > value and 'stalled' not in m.state:
                self.busyWait()
        m.stop()

    def turnOnRegulatedMotor(self, port, value):
        m = self.cfg['actors'][port]
        m.run_forever(speed_sp=self.scaleSpeed(m, clamp(value, -100, 100)))

    def turnOnUnregulatedMotor(self, port, value):
        value = clamp(value, -100, 100)
        self.cfg['actors'][port].run_direct(duty_cycle_sp=int(value))

    def setRegulatedMotorSpeed(self, port, value):
        m = self.cfg['actors'][port]
        # https://github.com/rhempel/ev3dev-lang-python/issues/263
        # m.speed_sp = self.scaleSpeed(m, clamp(value, -100, 100))
        m.run_forever(speed_sp=self.scaleSpeed(m, clamp(value, -100, 100)))

    def setUnregulatedMotorSpeed(self, port, value):
        value = clamp(value, -100, 100)
        self.cfg['actors'][port].duty_cycle_sp = int(value)

    def getRegulatedMotorSpeed(self, port):
        m = self.cfg['actors'][port]
        return m.speed * 100.0 / m.max_speed

    def getUnregulatedMotorSpeed(self, port):
        return self.cfg['actors'][port].duty_cycle

    def stopMotor(self, port, mode='float'):
        # mode: float, nonfloat
        # stop_actions: ['brake', 'coast', 'hold']
        m = self.cfg['actors'][port]
        if mode == 'float':
            m.stop_action = 'coast'
        elif mode == 'nonfloat':
            m.stop_action = 'brake'
        m.stop()

    def stopMotors(self, left_port, right_port):
        self.stopMotor(left_port)
        self.stopMotor(right_port)

    def stopAllMotors(self):
        # [m for m in [Motor(port) for port in ['outA', 'outB', 'outC', 'outD']] if m.connected]
        for file in glob.glob('/sys/class/tacho-motor/motor*/command'):
            with open(file, 'w') as f:
                f.write('stop')
        for file in glob.glob('/sys/class/dc-motor/motor*/command'):
            with open(file, 'w') as f:
                f.write('stop')

    def resetAllOutputs(self):
        for port in (ev3dev.OUTPUT_A, ev3dev.OUTPUT_B, ev3dev.OUTPUT_C, ev3dev.OUTPUT_D):
            lp = ev3dev.LegoPort(port)
            lp.mode = 'auto'

    def regulatedDrive(self, left_port, right_port, reverse, direction, speed_pct):
        # direction: forward, backward
        # reverse: always false for now
        speed_pct = clamp(speed_pct, -100, 100)
        ml = self.cfg['actors'][left_port]
        mr = self.cfg['actors'][right_port]
        if direction == 'backward':
            speed_pct = -speed_pct
        ml.run_forever(speed_sp=self.scaleSpeed(ml, speed_pct))
        mr.run_forever(speed_sp=self.scaleSpeed(mr, speed_pct))

    def driveDistance(self, left_port, right_port, reverse, direction, speed_pct, distance):
        # direction: forward, backward
        # reverse: always false for now
        speed_pct = clamp(speed_pct, -100, 100)
        ml = self.cfg['actors'][left_port]
        mr = self.cfg['actors'][right_port]
        circ = math.pi * self.cfg['wheel-diameter']
        dc = distance / circ
        if direction == 'backward':
            dc = -dc
        # set all attributes
        ml.stop_action = 'brake'
        ml.position_sp = int(dc * ml.count_per_rot)
        ml.speed_sp = self.scaleSpeed(ml, speed_pct)
        mr.stop_action = 'brake'
        mr.position_sp = int(dc * mr.count_per_rot)
        mr.speed_sp = self.scaleSpeed(mr, speed_pct)
        # start motors
        ml.run_to_rel_pos()
        mr.run_to_rel_pos()
        # logger.debug("driving: %s, %s" % (ml.state, mr.state))
        while ml.state or mr.state:
            self.busyWait()

    def rotateDirectionRegulated(self, left_port, right_port, reverse, direction, speed_pct):
        # direction: left, right
        # reverse: always false for now
        speed_pct = clamp(speed_pct, -100, 100)
        ml = self.cfg['actors'][left_port]
        mr = self.cfg['actors'][right_port]
        if direction == 'left':
            mr.run_forever(speed_sp=self.scaleSpeed(mr, speed_pct))
            ml.run_forever(speed_sp=self.scaleSpeed(ml, -speed_pct))
        else:
            ml.run_forever(speed_sp=self.scaleSpeed(ml, speed_pct))
            mr.run_forever(speed_sp=self.scaleSpeed(mr, -speed_pct))

    def rotateDirectionAngle(self, left_port, right_port, reverse, direction, speed_pct, angle):
        # direction: left, right
        # reverse: always false for now
        speed_pct = clamp(speed_pct, -100, 100)
        ml = self.cfg['actors'][left_port]
        mr = self.cfg['actors'][right_port]
        circ = math.pi * self.cfg['track-width']
        distance = angle * circ / 360.0
        circ = math.pi * self.cfg['wheel-diameter']
        dc = distance / circ
        logger.debug("doing %lf rotations" % dc)
        # set all attributes
        ml.stop_action = 'brake'
        ml.speed_sp = self.scaleSpeed(ml, speed_pct)
        mr.stop_action = 'brake'
        mr.speed_sp = self.scaleSpeed(mr, speed_pct)
        if direction == 'left':
            mr.position_sp = int(dc * mr.count_per_rot)
            ml.position_sp = int(-dc * ml.count_per_rot)
        else:
            ml.position_sp = int(dc * ml.count_per_rot)
            mr.position_sp = int(-dc * mr.count_per_rot)
        # start motors
        ml.run_to_rel_pos()
        mr.run_to_rel_pos()
        logger.debug("turning: %s, %s" % (ml.state, mr.state))
        while ml.state or mr.state:
            self.busyWait()

    def driveInCurve(self, direction, left_port, left_speed_pct, right_port, right_speed_pct, distance=None):
        # direction: foreward, backward
        ml = self.cfg['actors'][left_port]
        mr = self.cfg['actors'][right_port]
        left_speed_pct = self.scaleSpeed(ml, clamp(left_speed_pct, -100, 100))
        right_speed_pct = self.scaleSpeed(mr, clamp(right_speed_pct, -100, 100))
        if distance:
            left_dc = right_dc = 0.0
            speed_pct = (left_speed_pct + right_speed_pct) / 2.0
            if speed_pct:
                circ = math.pi * self.cfg['wheel-diameter']
                dc = distance / circ
                left_dc = dc * left_speed_pct / speed_pct
                right_dc = dc * right_speed_pct / speed_pct
            # set all attributes
            ml.stop_action = 'brake'
            ml.speed_sp = int(left_speed_pct)
            mr.stop_action = 'brake'
            mr.speed_sp = int(right_speed_pct)
            if direction == 'backward':
                ml.position_sp = int(-left_dc * ml.count_per_rot)
                mr.position_sp = int(-right_dc * mr.count_per_rot)
            else:
                ml.position_sp = int(left_dc * ml.count_per_rot)
                mr.position_sp = int(right_dc * mr.count_per_rot)
            # start motors
            ml.run_to_rel_pos()
            mr.run_to_rel_pos()
            while (ml.state and left_speed_pct) or (mr.state and right_speed_pct):
                self.busyWait()
        else:
            if direction == 'backward':
                ml.run_forever(speed_sp=int(-left_speed_pct))
                mr.run_forever(speed_sp=int(-right_speed_pct))
            else:
                ml.run_forever(speed_sp=int(left_speed_pct))
                mr.run_forever(speed_sp=int(right_speed_pct))

    # sensors
    def scaledValue(self, sensor):
        return sensor.value() / float(10.0 ** sensor.decimals)

    def scaledValues(self, sensor):
        scale = float(10.0 ** sensor.decimals)
        return tuple([sensor.value(i) / scale for i in range(sensor.num_values)])

    # touch sensor
    def isPressed(self, port):
        return self.scaledValue(self.cfg['sensors'][port])

    # ultrasonic sensor
    def getUltraSonicSensorDistance(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'US-DIST-CM':
            s.mode = 'US-DIST-CM'
        return self.scaledValue(s)

    def getUltraSonicSensorPresence(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'US-LISTEN':
            s.mode = 'US-LISTEN'
        return self.scaledValue(s) != 0.0

    # gyro
    # http://www.ev3dev.org/docs/sensors/lego-ev3-gyro-sensor/
    def resetGyroSensor(self, port):
        # change mode to reset for GYRO-ANG and GYRO-G&A
        s = self.cfg['sensors'][port]
        s.mode = 'GYRO-RATE'
        s.mode = 'GYRO-ANG'

    def getGyroSensorValue(self, port, mode):
        s = self.cfg['sensors'][port]
        if s.mode != Hal.GYRO_MODES[mode]:
            s.mode = Hal.GYRO_MODES[mode]
        return self.scaledValue(s)

    # color
    # http://www.ev3dev.org/docs/sensors/lego-ev3-color-sensor/
    def getColorSensorAmbient(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'COL-AMBIENT':
            s.mode = 'COL-AMBIENT'
        return self.scaledValue(s)

    def getColorSensorColour(self, port):
        colors = ['none', 'black', 'blue', 'green', 'yellow', 'red', 'white', 'brown']
        s = self.cfg['sensors'][port]
        if s.mode != 'COL-COLOR':
            s.mode = 'COL-COLOR'
        return colors[int(self.scaledValue(s))]

    def getColorSensorRed(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'COL-REFLECT':
            s.mode = 'COL-REFLECT'
        return self.scaledValue(s)

    def getColorSensorRgb(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'RGB-RAW':
            s.mode = 'RGB-RAW'
        return self.scaledValues(s)

    # infrared
    # http://www.ev3dev.org/docs/sensors/lego-ev3-infrared-sensor/
    def getInfraredSensorSeek(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'IR-SEEK':
            s.mode = 'IR-SEEK'
        return self.scaledValues(s)

    def getInfraredSensorDistance(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'IR-PROX':
            s.mode = 'IR-PROX'
        return self.scaledValue(s)

    # timer
    def getTimerValue(self, timer):
        if timer in self.timers:
            return int((time.time() - self.timers[timer]) * 1000.0)
        else:
            self.timers[timer] = time.time()
            return 0

    def resetTimer(self, timer):
        self.timers[timer] = time.time()

    # tacho-motor position
    def resetMotorTacho(self, actorPort):
        m = self.cfg['actors'][actorPort]
        m.position = 0

    def getMotorTachoValue(self, actorPort, mode):
        m = self.cfg['actors'][actorPort]
        tacho_count = m.position

        if mode == 'degree':
            return tacho_count * 360.0 / float(m.count_per_rot)
        elif mode in ['rotation', 'distance']:
            rotations = float(tacho_count) / float(m.count_per_rot)
            if mode == 'rotation':
                return rotations
            else:
                distance = round(math.pi * self.cfg['wheel-diameter'] * rotations)
                return distance
        else:
            raise ValueError('incorrect MotorTachoMode: %s' % mode)

    def getSoundLevel(self, port):
        # 100 for silent,
        # 0 for loud
        s = self.cfg['sensors'][port]
        if s.mode != 'DB':
            s.mode = 'DB'
        return round(-self.scaledValue(s) + 100, 2)  # map to 0 silent 100 loud

    def getHiTecCompassSensorValue(self, port, mode):
        s = self.cfg['sensors'][port]
        if s.mode != 'COMPASS':
            s.mode = 'COMPASS'  # ev3dev currently only supports the compass mode
        value = self.scaledValue(s)
        if mode == 'angle':
            return -(((value + 180) % 360) - 180)  # simulate the angle [-180, 180] mode from ev3lejos
        else:
            return value

    def getHiTecIRSeekerSensorValue(self, port, mode):
        s = self.cfg['sensors'][port]
        if s.mode != mode:
            s.mode = mode
        value = self.scaledValue(s)
        # remap from [1 - 9] default 0 to [120, -120] default NaN like ev3lejos
        return float('nan') if value == 0 else (value - 5) * -30

    def getHiTecColorSensorV2Colour(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'COLOR':
            s.mode = 'COLOR'
        value = s.value()
        return self.mapHiTecColorIdToColor(int(value))

    def mapHiTecColorIdToColor(self, id):
        if id < 0 or id > 17:
            return 'none'
        colors = {
            0: 'black',
            1: 'red',
            2: 'blue',
            3: 'blue',
            4: 'green',
            5: 'yellow',
            6: 'yellow',
            7: 'red',
            8: 'red',
            9: 'red',
            10: 'red',
            11: 'white',
            12: 'white',
            13: 'white',
            14: 'white',
            15: 'white',
            16: 'white',
            17: 'white',
        }
        return colors[id]

    def getHiTecColorSensorV2Ambient(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'PASSIVE':
            s.mode = 'PASSIVE'
        value = abs(s.value(0)) / 380
        return min(value, 100)

    def getHiTecColorSensorV2Light(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'NORM':
            s.mode = 'NORM'
        value = self.scaledValues(s)[3] / 2.55
        return value

    def getHiTecColorSensorV2Rgb(self, port):
        s = self.cfg['sensors'][port]
        if s.mode != 'NORM':
            s.mode = 'NORM'
        value = self.scaledValues(s)
        value = list(value)
        del value[0]
        return value

    def setHiTecColorSensorV2PowerMainsFrequency(self, port, frequency):
        s = self.cfg['sensors'][port]
        s.command = frequency

    # communication
    def _isTimeOut(self, e):
        # BluetoothError seems to be an IOError, which is an OSError
        # but all they do is: raise BluetoothError (str (e))
        return str(e) == "timed out"

    def establishConnectionTo(self, host):
        # host can also be a name, resolving it is slow though and requires the
        # device to be visible
        if not bluetooth.is_valid_address(host):
            nearby_devices = bluetooth.discover_devices()
            for bdaddr in nearby_devices:
                if host == bluetooth.lookup_name(bdaddr):
                    host = bdaddr
                    break
        if bluetooth.is_valid_address(host):
            con = BluetoothSocket(bluetooth.RFCOMM)
            con.settimeout(0.5)  # half second to make IO interruptible
            while True:
                try:
                    con.connect((host, 1))  # 0 is channel
                    self.bt_connections.append(con)
                    return len(self.bt_connections) - 1
                except bluetooth.btcommon.BluetoothError as e:
                    if not self._isTimeOut(e):
                        logger.error("unhandled Bluetooth error: %s", repr(e))
                        break
        else:
            return -1

    def waitForConnection(self):
        # enable visibility
        if not self.sys_bus:
            self.sys_bus = dbus.SystemBus()
        # do only once (since we turn off the timeout)
        # alternatively set DiscoverableTimeout = 0 in /etc/bluetooth/main.conf
        # and run hciconfig hci0 piscan, from robertalab initscript
        hci0 = self.sys_bus.get_object('org.bluez', '/org/bluez/hci0')
        props = dbus.Interface(hci0, 'org.freedesktop.DBus.Properties')
        props.Set('org.bluez.Adapter1', 'DiscoverableTimeout', dbus.UInt32(0))
        props.Set('org.bluez.Adapter1', 'Discoverable', True)

        if not self.bt_server:
            self.bt_server = BluetoothSocket(bluetooth.RFCOMM)
            self.bt_server.settimeout(0.5)  # half second to make IO interruptible
            self.bt_server.bind(("", bluetooth.PORT_ANY))
            self.bt_server.listen(1)

        while True:
            try:
                (con, info) = self.bt_server.accept()
                con.settimeout(0.5)  # half second to make IO interruptible
                self.bt_connections.append(con)
                return len(self.bt_connections) - 1
            except bluetooth.btcommon.BluetoothError as e:
                if not self._isTimeOut(e):
                    logger.error("unhandled Bluetooth error: %s", repr(e))
                    break
        return -1

    def readMessage(self, con_ix):
        message = "NO MESSAGE"
        if con_ix < len(self.bt_connections) and self.bt_connections[con_ix]:
            con = self.bt_connections[con_ix]
            logger.debug('reading msg')
            while True:
                # TODO(ensonic): how much do we actually expect
                # here is the lejos counter part
                # https://github.com/OpenRoberta/robertalab-ev3lejos/blob/master/
                # EV3Runtime/src/main/java/de/fhg/iais/roberta/runtime/ev3/BluetoothComImpl.java#L40..L59
                try:
                    message = con.recv(128).decode('utf-8', errors='replace')
                    logger.debug('received msg [%s]' % message)
                    break
                except bluetooth.btcommon.BluetoothError as e:
                    if not self._isTimeOut(e):
                        logger.error("unhandled Bluetooth error: %s", repr(e))
                        self.bt_connections[con_ix] = None
                        break
        return message

    def sendMessage(self, con_ix, message):
        if con_ix < len(self.bt_connections) and self.bt_connections[con_ix]:
            logger.debug('sending msg [%s]' % message)
            con = self.bt_connections[con_ix]
            while True:
                try:
                    con.send(message)
                    logger.debug('sent msg')
                    break
                except bluetooth.btcommon.BluetoothError as e:
                    if not self._isTimeOut(e):
                        logger.error("unhandled Bluetooth error: %s", repr(e))
                        self.bt_connections[con_ix] = None
                        break
