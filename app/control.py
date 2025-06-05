import sys
from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore as qtc
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QDesktopWidget

import board
import busio
from digitalio import Direction, Pull
from RPi import GPIO
from adafruit_mcp230xx.mcp23017 import MCP23017

from model import Model

class MainWindow(qtw.QMainWindow): 
    # These signals are internal to control.py
    startPressed = qtc.pyqtSignal()
    plugEventDetected = qtc.pyqtSignal()
    plugInToHandle = qtc.pyqtSignal(int)
    unPlugToHandle = qtc.pyqtSignal(int)
    
    # NEW: Thread-safe signals for GPIO operations
    gpioInterruptSignal = qtc.pyqtSignal(int, bool)  # pin, value
    startWatchdogSignal = qtc.pyqtSignal()
    stopWatchdogSignal = qtc.pyqtSignal()
    
    awaitingRestart = False
    interrupt = 17

    def __init__(self):
        super().__init__()

        # ------- pyqt window ----
        self.setWindowTitle("You Are the Operator")
        self.label = qtw.QLabel(self)
        self.label.setWordWrap(True)

        self.label.setAlignment(qtc.Qt.AlignTop)
        # Set margins using stylesheet
        self.label.setStyleSheet("""
            QLabel {
                margin-left: 30px;
                margin-top: 20px;
            }
        """)
        # Large text
        self.label.setFont(QFont('Arial',30))

        # Get screen dimensions
        screen = QDesktopWidget().screenGeometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        # Calculate position and size based on percentages
        height = int(screen_height * 0.3)  # 30% of screen height
        y = int(screen_height - height)  # Bottom of screen
        
        # Apply geometry
        self.setGeometry(0, y, screen_width, height)

        self.setCentralWidget(self.label)
        self.model = Model()

        # --- Race condition detection --- 
        self.interrupt_timestamps = []  # Track interrupt timing
        self.max_interrupts_per_second = 20  # More lenient threshold
        self.event_queue = []  # Track pending events
        self.max_queue_size = 10  # Maximum pending events
        self.just_checked_time = None  # Track when just_checked was set
        
        # Watchdog timer for stuck states
        self.watchdogTimer = qtc.QTimer()
        self.watchdogTimer.timeout.connect(self.watchdogTimeout)
        self.watchdogTimer.setInterval(5000)  # 5 second timeout
        self.lastActivityTime = qtc.QTime.currentTime()
        
        # Chaos detection timer
        self.chaosDetectionTimer = qtc.QTimer()
        self.chaosDetectionTimer.timeout.connect(self.checkForChaos)
        self.chaosDetectionTimer.start(500)  # Check every 500ms

        # --- timers --- 
        self.bounceTimer = qtc.QTimer()
        self.bounceTimer.timeout.connect(self.continueCheckPin)
        self.bounceTimer.setSingleShot(True)
        
        self.blinkTimer = qtc.QTimer()
        self.blinkTimer.timeout.connect(self.blinker)

        self.captionTimer = qtc.QTimer()
        self.captionTimer.setSingleShot(True)
        self.captionTimer.timeout.connect(self.display_next_caption)
        self.captionIndex = 0
        self.captions = 'empty'
        self.areCaptionsContinuing = True

        # Connect signals
        self.startPressed.connect(self.startSim)
        self.plugEventDetected.connect(lambda: self.bounceTimer.start(300))
        self.plugInToHandle.connect(self.model.handlePlugIn)
        self.unPlugToHandle.connect(self.model.handleUnPlug)

        # Events from model.py
        self.model.displayTextSignal.connect(self.displayText)
        self.model.setLEDSignal.connect(self.setLED)
        self.model.blinkerStart.connect(self.startBlinker)
        self.model.blinkerStop.connect(self.stopBlinker)
        self.model.displayCaptionSignal.connect(self.displayCaptions)
        self.model.stopCaptionSignal.connect(self.stopCaptions)
        self.model.stopSimSignal.connect(self.stopSim)
        
        # NEW: Connect thread-safe GPIO signals
        self.gpioInterruptSignal.connect(self.handleGpioInterrupt)
        self.startWatchdogSignal.connect(lambda: self.watchdogTimer.start())
        self.stopWatchdogSignal.connect(lambda: self.watchdogTimer.stop())

        # Initialize the I2C bus:
        i2c = busio.I2C(board.SCL, board.SDA)
        self.mcp = MCP23017(i2c)  # default address-0x20
        self.mcpLed = MCP23017(i2c, address=0x21)

        # Make a list of pins for each bonnet
        self.pins = []
        for pinIndex in range(0, 16):
            self.pins.append(self.mcp.get_pin(pinIndex))

        # LEDs 
        self.pinsLed = []
        for pinIndex in range(0, 12):
            self.pinsLed.append(self.mcpLed.get_pin(pinIndex))

        # Set up Tip interrupt
        self.mcp.interrupt_enable = 0xFFFF  # Enable Interrupts in all pins
        self.mcp.interrupt_configuration = 0x0000  # interrupt on any change
        self.mcp.io_control = 0x44  # Interrupt as open drain and mirrored
        
        self.mcp.clear_ints()  # Interrupts need to be cleared initially
        
        # Do initial reset but don't set up interrupts yet
        self.reset(skip_gpio_setup=True)

        # Set up GPIO - but delay the actual interrupt setup
        self.interrupt = 17
        GPIO.setmode(GPIO.BCM)

        # Remove any existing event detection
        try:
            GPIO.remove_event_detect(self.interrupt)
        except:
            pass

        GPIO.setup(self.interrupt, GPIO.IN, GPIO.PUD_UP)
        
        # Delay GPIO interrupt setup to ensure everything is initialized
        qtc.QTimer.singleShot(500, self.setupGpioInterrupts)

    def checkPin(self, port):
        """GPIO callback - runs in interrupt thread, so we use signals"""
        # Get current time in thread-safe way
        current_time_ms = qtc.QDateTime.currentMSecsSinceEpoch()
        
        # Read interrupt flags
        try:
            int_flags = self.mcp.int_flag
            for pin_flag in int_flags:
                # Read pin value
                pin_value = self.pins[pin_flag].value
                # Emit signal to main thread
                self.gpioInterruptSignal.emit(pin_flag, pin_value)
        except Exception as e:
            print(f"Error in GPIO interrupt: {e}")

    def handleGpioInterrupt(self, pin_flag, pin_value):
        """This runs in the main thread and can safely use timers"""
        current_time = qtc.QTime.currentTime()
        
        # Track interrupt frequency
        self.interrupt_timestamps.append(current_time)
        # Keep only last second of timestamps
        one_second_ago = current_time.addSecs(-1)
        self.interrupt_timestamps = [t for t in self.interrupt_timestamps 
                                    if t > one_second_ago]
        
        # Check for interrupt storm (more lenient threshold)
        if len(self.interrupt_timestamps) > self.max_interrupts_per_second:
            print(f"CHAOS DETECTED: {len(self.interrupt_timestamps)} interrupts/second!")
            self.handleChaos("Interrupt storm detected")
            return
        
        print(f"* Interrupt - pin number: {pin_flag} changed to: {pin_value}")
        
        if pin_flag < 12:
            if not self.just_checked:
                # Check if we already have a recent event for this pin
                duplicate = False
                for event in self.event_queue:
                    if (event['pin'] == pin_flag and 
                        event['time'].msecsTo(current_time) < 100):  # Within 100ms
                        duplicate = True
                        print(f"  (Ignoring duplicate interrupt for pin {pin_flag})")
                        break
                
                if not duplicate:
                    self.pinFlag = pin_flag
                    # Track this event
                    self.event_queue.append({
                        'time': current_time,
                        'pin': pin_flag,
                        'value': pin_value
                    })
                    # Only check queue size if it's getting really big
                    if len(self.event_queue) > self.max_queue_size:
                        print(f"CHAOS DETECTED: Event queue overflow ({len(self.event_queue)} events)")
                        self.handleChaos("Event queue overflow")
                        return
                    # Start watchdog only when we add events to process
                    if not self.watchdogTimer.isActive():
                        self.watchdogTimer.start()
                    self.plugEventDetected.emit()
            else:
                print(f"  (Ignoring interrupt - just_checked is True)")
        else:
            # Handle button presses
            if pin_flag == 13 and pin_value == False:
                self.startPressed.emit()
            elif pin_flag == 12:
                print(f'   * got to stop, aka pin 12, {pin_value}')
                self.stopSim()

    def stopSim(self):
        print('stopping sim')
        self.label.setText("The Switchboard has stopped. Press the Start button to begin!")
        self.stopMedia()

    def startSim(self):
        self.stopMedia()
        if self.getAnyPinsIn():
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
        
        # Stop all timers
        if self.bounceTimer.isActive():
            self.bounceTimer.stop()
        if self.blinkTimer.isActive():
            self.blinkTimer.stop()            
        if self.captionTimer.isActive():
            self.captionTimer.stop()
        if self.watchdogTimer.isActive():
            self.watchdogTimer.stop()
        if self.chaosDetectionTimer.isActive():
            self.chaosDetectionTimer.stop()

    def setupGpioInterrupts(self):
        """Set up GPIO interrupts after initialization is complete"""
        print("Setting up GPIO interrupts...")
        
        # Clear any pending interrupts first
        self.mcp.clear_ints()
        
        # Read all pins to establish baseline
        for i in range(16):
            try:
                _ = self.pins[i].value
            except:
                pass
        
        # Clear interrupts again after reading
        self.mcp.clear_ints()
        
        # Now add the interrupt handler
        GPIO.add_event_detect(self.interrupt, GPIO.BOTH, callback=self.checkPin, bouncetime=50)
        print("GPIO interrupts ready")
        
    def reset(self):
        # Clear chaos detection state
        self.event_queue.clear()
        self.interrupt_timestamps.clear()
        self.watchdogTimer.stop()
        
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

        # Set to input
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

        # Stop timers
        if self.bounceTimer.isActive():
            self.bounceTimer.stop()
        if self.blinkTimer.isActive():
            self.blinkTimer.stop()            
        if self.captionTimer.isActive():
            self.captionTimer.stop()  

        # Reconfigure the MCP23017 interrupt system
        self.mcp.interrupt_configuration = 0x0000
        self.mcp.io_control = 0x44
        self.mcp.clear_ints()
        
        # Set up the GPIO pin again
        GPIO.setup(self.interrupt, GPIO.IN, GPIO.PUD_UP)
        
        # Small delay before re-adding interrupts
        qtc.QTimer.singleShot(100, lambda: GPIO.add_event_detect(
            self.interrupt, GPIO.BOTH, callback=self.checkPin, bouncetime=50))
        
        # Restart chaos detection
        self.chaosDetectionTimer.start(500)

    def continueCheckPin(self):
        """Modified to remove processed events and detect stuck states"""
        print(f" * In continue, pinFlag = {str(self.pinFlag)} " 
              f"  * value: {str(self.pins[self.pinFlag].value)}")
        
        # Mark that we're processing this pin
        self.just_checked = True
        
        # Remove ALL events for this pin from queue
        before_count = len(self.event_queue)
        self.event_queue = [e for e in self.event_queue if e['pin'] != self.pinFlag]
        after_count = len(self.event_queue)
        if before_count != after_count:
            print(f"   Removed {before_count - after_count} events for pin {self.pinFlag}")
        
        # Stop watchdog if queue is now empty
        if len(self.event_queue) == 0:
            self.watchdogTimer.stop()
        
        # Check for conflicting states
        if self.detectConflictingStates():
            self.handleChaos("Conflicting pin states detected")
            return

        if self.awaitingRestart:
            print(' * awaiting restart')
        else:
            # Plug-in
            if self.pins[self.pinFlag].value == False: 
                # grounded by tip, aka connected
                self.plugInToHandle.emit(self.pinFlag)
            # Unplug
            else:
                # was this a legit unplug?
                if self.model.getIsPinIn(self.pinFlag):
                    print(f" * pin {self.pinFlag} was in - handleUnPlug")
                    self.unPlugToHandle.emit(self.pinFlag)
                else:
                    print(" ** got to pin true (changed to high), but not pin in")

        # Delay setting just_checked to false
        qtc.QTimer.singleShot(150, self.delayedFinishCheck)

    def delayedFinishCheck(self):
        print(" * delayed finished check")
        self.just_checked = False
        
        # Clean up any events for the pin we just processed
        if hasattr(self, 'pinFlag'):
            before_count = len(self.event_queue)
            self.event_queue = [e for e in self.event_queue if e['pin'] != self.pinFlag]
            after_count = len(self.event_queue)
            if before_count != after_count:
                print(f"   Cleaned up {before_count - after_count} events for pin {self.pinFlag}")
        
        # Stop watchdog if queue is empty
        if len(self.event_queue) == 0 and self.watchdogTimer.isActive():
            self.watchdogTimer.stop()
            print("   Watchdog stopped - queue empty")
        
        # Experimental
        self.mcp.clear_ints()  # This seems to keep things fresh

    def displayText(self, msg):
        self.label.setText(msg)        

    def setLED(self, flagIdx, onOrOff):
        self.pinsLed[flagIdx].value = onOrOff     

    def blinker(self):
        if self.pinToBlink < len(self.pinsLed):
            self.pinsLed[self.pinToBlink].value = not self.pinsLed[self.pinToBlink].value
        
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
        if self.captionTimer.isActive():
            self.captionTimer.stop()

    def time_str_to_ms(self, time_str):
        hours, minutes, seconds_ms = time_str.split(':')
        seconds, milliseconds = seconds_ms.split(',')
        return int(hours) * 3600000 + int(minutes) * 60000 + int(seconds) * 1000 + int(milliseconds)

    def displayCaptions(self, fileType, file_name):
        try:
            with open('captions/' + fileType + '/' + file_name + '.srt', 'r') as f:
                self.captions = f.read().split('\n\n')
            self.areCaptionsContinuing = True
            self.captionIndex = 0
            self.display_next_caption()
        except Exception as e:
            print(f"Error loading captions: {e}")

    def display_next_caption(self):
        if self.captionIndex < len(self.captions) and self.areCaptionsContinuing:
            caption = self.captions[self.captionIndex]
            if '-->' in caption:
                try:
                    number, time, text = caption.split('\n', 2)
                    if self.areCaptionsContinuing:
                        self.displayText(text)
                    # Process time
                    times = time.split(' --> ')
                    start_time_ms = self.time_str_to_ms(times[0])
                    end_time_ms = self.time_str_to_ms(times[1])
                    duration_ms = end_time_ms - start_time_ms
                    if self.areCaptionsContinuing:
                        self.captionTimer.start(duration_ms)
                except Exception as e:
                    print(f"Error processing caption: {e}")
            self.captionIndex += 1

    def detectConflictingStates(self):
        """Detect impossible or conflicting pin states"""
        # Check for rapid state changes on same pin
        if len(self.event_queue) >= 2:
            recent_events = self.event_queue[-2:]
            if (recent_events[0]['pin'] == recent_events[1]['pin'] and
                recent_events[0]['value'] != recent_events[1]['value'] and
                recent_events[1]['time'].msecsTo(recent_events[0]['time']) < 50):
                print("Conflicting states: Same pin changed too quickly")
                return True
        
        # Check model state consistency
        if hasattr(self.model, 'phoneLine'):
            line = self.model.phoneLine
            # Both parties can't be unplugged while line is engaged
            if (line['isEngaged'] and 
                not line['caller']['isPlugged'] and 
                not line['callee']['isPlugged']):
                print("Conflicting states: Line engaged but both unplugged")
                return True
            
            # Can't have callee plugged without caller in normal circumstances
            if (not line['caller']['isPlugged'] and 
                line['callee']['isPlugged'] and
                line['unPlugStatus'] == self.model.NO_UNPLUG_STATUS):
                print("Conflicting states: Callee without caller")
                return True
        
        return False

    def checkForChaos(self):
        """Periodic check for stuck states"""
        # Check if we have old unprocessed events
        if self.event_queue:
            # Clean up any events that match currently processing pin
            if hasattr(self, 'pinFlag') and self.just_checked:
                self.event_queue = [e for e in self.event_queue if e['pin'] != self.pinFlag]
            
            # Now check for stuck events
            if self.event_queue:
                oldest_event = self.event_queue[0]
                age = oldest_event['time'].msecsTo(qtc.QTime.currentTime())
                
                # Only consider it stuck if:
                # 1. It's REALLY old (5+ seconds to be extra safe) AND
                # 2. We're not currently processing it (bounceTimer not active) AND
                # 3. We're not in the just_checked state AND
                # 4. It's not the pin we're currently working on
                if (age > 5000 and 
                    not self.bounceTimer.isActive() and 
                    not self.just_checked and
                    (not hasattr(self, 'pinFlag') or oldest_event['pin'] != self.pinFlag)):
                    print(f"CHAOS DETECTED: Stuck event (age: {age}ms)")
                    print(f"  Event details: pin={oldest_event['pin']}, value={oldest_event['value']}")
                    print(f"  BounceTimer active: {self.bounceTimer.isActive()}")
                    print(f"  just_checked: {self.just_checked}")
                    print(f"  Current pinFlag: {getattr(self, 'pinFlag', 'None')}")
                    self.handleChaos("Stuck event detected")
                    return
        
        # Check for stuck timers
        stuck_timers = []
        if self.bounceTimer.isActive():
            remaining = self.bounceTimer.remainingTime()
            if remaining < -2000:  # Timer should have fired 2+ seconds ago
                stuck_timers.append(f"bounceTimer (remaining: {remaining}ms)")
        
        if hasattr(self.model, 'callInitTimer') and self.model.callInitTimer.isActive():
            remaining = self.model.callInitTimer.remainingTime()
            if remaining < -2000:
                stuck_timers.append(f"callInitTimer (remaining: {remaining}ms)")
        
        if stuck_timers:
            print(f"CHAOS DETECTED: Stuck timers: {stuck_timers}")
            self.handleChaos(f"Stuck timers: {', '.join(stuck_timers)}")

    def watchdogTimeout(self):
        """Called if no activity for 5 seconds during active operation"""
        # Only trigger if there are unprocessed events waiting
        # OR if we're in a transition state that should have resolved
        should_trigger = False
        
        # Check for stuck events in queue
        if len(self.event_queue) > 0:
            oldest_event = self.event_queue[0]
            age = oldest_event['time'].msecsTo(qtc.QTime.currentTime())
            if age > 3000:  # Event older than 3 seconds
                should_trigger = True
                print(f"Watchdog: Stuck event detected, age {age}ms")
        
        # Check for stuck bounce timer
        if self.bounceTimer.isActive() and self.bounceTimer.remainingTime() < -1000:
            should_trigger = True
            print("Watchdog: Bounce timer stuck")
        
        # Check for inconsistent state (e.g., just_checked stuck true for too long)
        if hasattr(self, 'just_checked') and self.just_checked:
            # just_checked should be cleared within 500ms normally
            if not hasattr(self, 'just_checked_time'):
                self.just_checked_time = qtc.QTime.currentTime()
            elif self.just_checked_time.msecsTo(qtc.QTime.currentTime()) > 1000:
                should_trigger = True
                print("Watchdog: just_checked stuck true")
        else:
            self.just_checked_time = None
        
        if should_trigger:
            print("WATCHDOG TIMEOUT: System appears frozen")
            self.handleChaos("System timeout - stuck state detected")

    def handleChaos(self, reason):
        """Central handler for all chaos situations"""
        print(f"\n*** CHAOS RECOVERY INITIATED ***")
        print(f"Reason: {reason}")
        print(f"Event queue size: {len(self.event_queue)}")
        print(f"Interrupts/sec: {len(self.interrupt_timestamps)}")
        
        # Print the message to console instead of screen (it flashes by too quickly)
        print("CHAOS MESSAGE: Oops! Things got a bit tangled.")
        print(f"CHAOS MESSAGE: {reason}")
        print("CHAOS MESSAGE: Press Stop then Start to try again.")
        
        # Clear all pending states
        self.event_queue.clear()
        self.interrupt_timestamps.clear()
        self.just_checked = False
        
        # Stop all timers
        self.bounceTimer.stop()
        self.watchdogTimer.stop()
        
        # Display a brief error message that stays visible
        self.displayText(f"System reset - press Stop then Start to continue")
        
        # Give the system a moment to settle
        qtc.QTimer.singleShot(500, self.forceSafeState)

    def forceSafeState(self):
        """Force system into a known safe state"""
        try:
            # Clear GPIO interrupts
            GPIO.remove_event_detect(self.interrupt)
            self.mcp.clear_ints()
            
            # Call stopSim to reset everything
            self.stopSim()
            
            # Re-add GPIO detection after a delay
            qtc.QTimer.singleShot(1000, self.reEnableGPIO)
            
        except Exception as e:
            print(f"Error in forceSafeState: {e}")
            # Last resort - just stop media
            self.stopMedia()

    def reEnableGPIO(self):
        """Re-enable GPIO after chaos recovery"""
        try:
            GPIO.setup(self.interrupt, GPIO.IN, GPIO.PUD_UP)
            GPIO.add_event_detect(self.interrupt, GPIO.BOTH, 
                                callback=self.checkPin, bouncetime=50)
            print("GPIO re-enabled after chaos recovery")
        except Exception as e:
            print(f"Error re-enabling GPIO: {e}")


app = qtw.QApplication([])
win = MainWindow()
win.show()
sys.exit(app.exec_())