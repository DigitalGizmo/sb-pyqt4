import sys
# import json
from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore as qtc
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QDesktopWidget

import vlc
import board
import busio
from digitalio import Direction, Pull
from RPi import GPIO
from adafruit_mcp230xx.mcp23017 import MCP23017

from model import Model

class MainWindow(qtw.QMainWindow): 
    # Most of this module is analogous to svelte Panel

    # These signals are internal to control.py
    startPressed = qtc.pyqtSignal()
    plugEventDetected = qtc.pyqtSignal()
    plugInToHandle = qtc.pyqtSignal(int)
    unPlugToHandle = qtc.pyqtSignal(int)
    # wiggleDetected = qtc.pyqtSignal()
    awaitingRestart = False
    interrupt = 17

    def __init__(self):
        # self.pygame.init()
        super().__init__()

        # ------- pyqt window ----
        self.setWindowTitle("You Are the Operator")
        self.label = qtw.QLabel(self)
        self.label.setWordWrap(True)
        # self.label.setText("Keep your ears open for incoming calls! ")

        self.label.setAlignment(qtc.Qt.AlignTop)
        # Set margins using stylesheet
        self.label.setStyleSheet("""
            QLabel {
                margin-left: 30px;
                margin-top: 20px;
            }
        """)
        # padding: 10px;
        # Large text
        self.label.setFont(QFont('Arial',30))


        # Get screen dimensions
        screen = QDesktopWidget().screenGeometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        # Calculate position and size based on percentages
        # width = int(screen_width * 0.8)  # 80% of screen width
        height = int(screen_height * 0.3)  # 60% of screen height
        # x = int((screen_width - width) / 2)  # Center horizontally
        # y = int((screen_height - height) / 2)  # Center vertically
        y = int(screen_height - height)  # Center vertically
        
        # Apply geometry
        self.setGeometry(0, y, screen_width, height)

        # # Small text for debug
        # self.label.setFont(QFont('Arial',16))
        # self.setGeometry(15,80,600,250)

        self.setCentralWidget(self.label)
        self.model = Model()

        # --- timers --- 
        self.bounceTimer=qtc.QTimer()
        self.bounceTimer.timeout.connect(self.continueCheckPin)
        self.bounceTimer.setSingleShot(True)
        self.blinkTimer=qtc.QTimer()
        self.blinkTimer.timeout.connect(self.blinker)

        self.captionTimer=qtc.QTimer()
        self.captionTimer.setSingleShot(True)
        self.captionTimer.timeout.connect(self.display_next_caption)
        self.captionIndex = 0
        self.captions = 'empty'
        self.areCaptionsContinuing = True

        # Supress interrupt when plug is just wiggled (disabled)
        # self.wiggleDetected.connect(lambda: self.wiggleTimer.start(80))
        # self.wiggleTimer=qtc.QTimer()
        # self.wiggleTimer.setSingleShot(True)
        # self.wiggleTimer.timeout.connect(self.checkWiggle)

        # Self (control) for gpio related, self.model for audio
        self.startPressed.connect(self.startSim)

        # self.startPressed.connect(self.model.handleStart)

        # Bounce timer less than 200 cause failure to detect 2nd line
        # Tested with 100
        self.plugEventDetected.connect(lambda: self.bounceTimer.start(300))
        self.plugInToHandle.connect(self.model.handlePlugIn)
        self.unPlugToHandle.connect(self.model.handleUnPlug)

        # Eventst from model.py
        self.model.displayTextSignal.connect(self.displayText)
        self.model.setLEDSignal.connect(self.setLED)
        # self.model.pinInEvent.connect(self.setPinsIn)
        self.model.blinkerStart.connect(self.startBlinker)
        self.model.blinkerStop.connect(self.stopBlinker)
        # self.model.checkPinsInEvent.connect(self.checkPinsIn)
        self.model.displayCaptionSignal.connect(self.displayCaptions)
        self.model.stopCaptionSignal.connect(self.stopCaptions)
        self.model.stopSimSignal.connect(self.stopSim)
   

        # Initialize the I2C bus:
        i2c = busio.I2C(board.SCL, board.SDA)
        self.mcp = MCP23017(i2c) # default address-0x20
        # self.mcpRing = MCP23017(i2c, address=0x22)
        self.mcpLed = MCP23017(i2c, address=0x21)

        # -- Make a list of pins for each bonnet, set input/output --
        # Plug tip, which will trigger interrupts
        self.pins = []
        for pinIndex in range(0, 16):
            self.pins.append(self.mcp.get_pin(pinIndex))
        # Will be initiallized to pull.up in reset()

        # LEDs 
        # Tried to put these in the Model/logic module -- but seems all gpio
        # needs to be in this base/main module
        self.pinsLed = []
        for pinIndex in range(0, 12):
            self.pinsLed.append(self.mcpLed.get_pin(pinIndex))
        # Set to output in reset()

        # -- Set up Tip interrupt --
        self.mcp.interrupt_enable = 0xFFFF  # Enable Interrupts in all pins
        # self.mcp.interrupt_enable = 0xFFF  # Enable Interrupts first 12 pins
        # self.mcp.interrupt_enable = 0b0000111111111111  # Enable Interrupts in pins 0-11 aka 0xfff

        # If intcon is set to 0's we will get interrupts on both
        #  button presses and button releases
        self.mcp.interrupt_configuration = 0x0000  # interrupt on any change
        self.mcp.io_control = 0x44  # Interrupt as open drain and mirrored
        # put this in startup?

        self.mcp.clear_ints()  # Interrupts need to be cleared initially
        self.reset()

        # Instead of defining checkPin inside __init__, use:
        self.interrupt = 17  # Define as an instance variable for reuse in reset()
        GPIO.setmode(GPIO.BCM)

        # First remove any existing event detection
        try:
            GPIO.remove_event_detect(self.interrupt)
        except:
            pass  # Handle exception if no event detection exists

        GPIO.setup(self.interrupt, GPIO.IN, GPIO.PUD_UP)
        GPIO.add_event_detect(self.interrupt, GPIO.BOTH, callback=self.checkPin, bouncetime=50)

    def checkPin(self, port):
        """Callback function to be called when an Interrupt occurs.
        The signal for pluginEventDetected calls a timer -- it can't send
        a parameter, so the work-around is to set pin_flag as a global.
        """
        for pin_flag in self.mcp.int_flag:
            # print("Interrupt connected to Pin: {}".format(port))
            print(f"* Interrupt - pin number: {pin_flag} changed to: {self.pins[pin_flag].value}")

            # Test for phone jack vs start and stop buttons
            if (pin_flag < 12):
                # Don't restart this interrupt checking if we're still
                # in the pause part of bounce checking
                if (not self.just_checked):
                    self.pinFlag = pin_flag
                    
                    self.plugEventDetected.emit()
                    # Starts bounceTimer which call continuePinCheck

            else:
                print(" * got to interupt 12 or greater \n")
                if (pin_flag == 13 and self.pins[13].value == False):
                    # if (self.pins[13].value == False):
                    self.startPressed.emit() # Calls stopMedia
                elif (pin_flag == 12):
                    print(f'   * got to stop, aka pin 12, ' + str(self.pins[12].value))
                    self.stopSim()
                # else:
                #     print(f' * pin_flag: ' + str(pin_flag))


    def stopSim(self):
        print('stopping sim')
        self.label.setText("The Switchboard has stopped. Press the Start button to begin!")

        self.stopMedia()
        
        # if (self.getAnyPinsIn()):
        #     self.label.setText("Remove phone plugs and when you're ready, press Start")
        # else:
        #     self.reset()
        #     print('press start to begin')
        #     # self.model.handleStart()        

    def startSim(self):
        self.stopMedia()
        if (self.getAnyPinsIn()):
            self.label.setText("Remove phone plugs and when you're ready, press Start")
        else:
            self.reset()
            self.model.handleStart()

    def stopMedia(self):
        print(" * resetting, starting")
        self.awaitingRestart = True
        self.stopCaptions()
        self.setLEDsOff()
        self.model.stopAllAudio()
        self.model.stopTimers()
        # Stop blinking
        if self.bounceTimer.isActive():
            self.bounceTimer.stop()
        if self.blinkTimer.isActive():
            self.blinkTimer.stop()            
        if self.captionTimer.isActive():
            self.captionTimer.stop()  

    def reset(self):
        # Remove the existing event detection
        try:
            GPIO.remove_event_detect(self.interrupt)
        except:
            pass
        # Clear interrupts
        self.mcp.clear_ints()
        
        self.label.setText("Press the Start button to begin!")
        self.just_checked = False
        self.pinFlag = 15
        self.pinToBlink = 0
        self.awaitingRestart = False
        self.captionIndex = 0

        # Synchronize pin states with model
        for pinIndex in range(0, 12):
            is_pin_in = self.pins[pinIndex].value == False
            self.model.setPinIn(pinIndex, is_pin_in)

        # Set to input - later will get intrrupt as well
        for pinIndex in range(0, 16):
            self.pins[pinIndex].direction = Direction.INPUT
            self.pins[pinIndex].pull = Pull.UP
        
        # Set LEDs to output and off
        for pinIndex in range(0, 12):
            self.pinsLed[pinIndex].switch_to_output(value=False)

        # Call model's reset
        self.model.reset()
        # Ensure all VLC event handlers are detached
        self.model.detachAllEventHandlers()

        # Maybe move these to stopMedia()
        # self.stopMedia()
        if self.bounceTimer.isActive():
            self.bounceTimer.stop()
        if self.blinkTimer.isActive():
            self.blinkTimer.stop()            
        if self.captionTimer.isActive():
            self.captionTimer.stop()  

        # Reconfigure the MCP23017 interrupt system
        self.mcp.interrupt_configuration = 0x0000  # interrupt on any change
        self.mcp.io_control = 0x44  # Interrupt as open drain and mirrored
        self.mcp.clear_ints()  # Final clear of interrupts
        
        # Set up the GPIO pin again before adding event detection
        GPIO.setup(self.interrupt, GPIO.IN, GPIO.PUD_UP)

        # Re-add the event detection
        GPIO.add_event_detect(self.interrupt, GPIO.BOTH, callback=self.checkPin, bouncetime=50)        

        # self.setLED(0, True)          
        # self.setLED(6, True)          
        # self.setLED(2, True)          

    def continueCheckPin(self):
        # Not able to send param through timer, so pinFlag has been set globaly
        print(f" * In continue, pinFlag = {str(self.pinFlag)} " 
              f"  * value: {str(self.pins[self.pinFlag].value)}")

        if (self.awaitingRestart):
            # do nothing - awaiting press of start button
            print(' * awaiting restart')
        else:
            # Plug-in
            if (self.pins[self.pinFlag].value == False): 
                # grounded by tip, aka connected
                """
                False/grouded, then this event is a plug-in
                """
                # Send pin index to model.py as an int 
                # Model uses signals for LED, text and pinsIn to set here
                self.plugInToHandle.emit(self.pinFlag)
            # Unplug
            else: # pin flag True, still, or again, high
                # aka not connected
                # print(f"  ** got to pin disconnected in continueCheckPin")

                # was this a legit unplug?
                if (self.model.getIsPinIn(self.pinFlag)):
                    # if this pin was in
                    # print(f"Pin {self.pinFlag} has been disconnected \n")
                    print(f" * pin {self.pinFlag} was in - handleUnPlug")

                    # On unplug we can't tell which line electonicaly 
                    # (diff in shaft is gone), so rely on pinsIn info
                    self.unPlugToHandle.emit(self.pinFlag) # , self.whichLinePlugging
                    # Model handleUnPlug will set pinsIn false for this on
                else:
                    print(" ** got to pin true (changed to high), but not pin in")

        # Delay setting just_check to false in case the plug is wiggled
        # qtc.QTimer.singleShot(300, self.delayedFinishCheck)
        # qtc.QTimer.singleShot(70, self.delayedFinishCheck)
        qtc.QTimer.singleShot(150, self.delayedFinishCheck)

    def delayedFinishCheck(self):
        # This just delay resetting just_checked
        print(" * delayed finished check \n")
        self.just_checked = False

        # Experimental
        self.mcp.clear_ints()  # This seems to keep things fresh


    # def checkWiggle(self):
    #     print(" * got to checkWiggle")
    #     # self.wiggleTimer.stop() -- now singleShot
    #     # Check whether the pin is still grounded aka False
    #     # if no longer grounded, proceed with event detection
    #     if (not self.pins[self.pinFlag].value == False):
    #         # The pin is no longer in
    #         self.just_checked = True
    #         self.plugEventDetected.emit()
    #     else: 
    #         # still grounded -- do nothing
    #         # pin has been removed during pause
    #         print(f'in wiggle-- not supposed to get here - grounded')

    def displayText(self, msg):
        self.label.setText(msg)        

    def setLED(self, flagIdx, onOrOff):
        self.pinsLed[flagIdx].value = onOrOff     

    def blinker(self):
        self.pinsLed[self.pinToBlink].value = not self.pinsLed[self.pinToBlink].value
        # print("blinking value: " + str(self.pinsLed[self.pinToBlink].value))
        
    def startBlinker(self, personIdx):
        self.pinToBlink = personIdx
        self.blinkTimer.start(600)

    def stopBlinker(self):
        if self.blinkTimer.isActive():
            self.blinkTimer.stop()

    def setLEDsOff(self):
        for pinIndex in range(0, 12):
            self.setLED(pinIndex, False)

    def getAnyPinsIn(self):
        anyPinsIn = False

        for pinIndex in range(0, 12):
            if self.pins[pinIndex].value == False:
                anyPinsIn = True
        return anyPinsIn

    def stopCaptions(self):
        self.areCaptionsContinuing = False
        self.captionTimer.stop()

    def time_str_to_ms(self, time_str):
        hours, minutes, seconds_ms = time_str.split(':')
        seconds, milliseconds = seconds_ms.split(',')
        return int(hours) * 3600000 + int(minutes) * 60000 + int(seconds) * 1000 + int(milliseconds)

    # Mostly from ChatGPT
    def displayCaptions(self, fileType, file_name):
        with open('captions/' + fileType + '/' + file_name + '.srt', 'r') as f:
            self.captions = f.read().split('\n\n')
        self.areCaptionsContinuing = True
        self.captionIndex = 0

        self.display_next_caption()

    def display_next_caption(self):
        # print('got to display_next_caption')
        # nonlocal self
        if self.captionIndex < len(self.captions):
            caption = self.captions[self.captionIndex]
            # print(f'full entry: {caption}')
            if '-->' in caption:
                number, time, text = caption.split('\n', 2)
                # print(f'#: {number} time: {time}, text: {text}')
                # Stop if unplugged
                if (self.areCaptionsContinuing):
                    self.displayText(text)
                # Proccess time
                times = time.split(' --> ')
                # print(f'times[0]: {times[0]}')
                start_time_ms = self.time_str_to_ms(times[0])
                end_time_ms = self.time_str_to_ms(times[1])
                duration_ms = end_time_ms - start_time_ms
                if (self.areCaptionsContinuing):
                    self.captionTimer.start(duration_ms)
            self.captionIndex += 1

app = qtw.QApplication([])

win = MainWindow()
win.show()

sys.exit(app.exec_())