# import sys
import json
from PyQt5 import QtWidgets as qtw
from PyQt5 import QtGui as qtg
from PyQt5 import QtCore as qtc
import vlc

conversationsJsonFile = open('conversations.json')
conversations = json.load(conversationsJsonFile)
personsJsonFile = open('persons.json')
persons = json.load(personsJsonFile)

class Model(qtc.QObject):
    """Main logic patterned after software proto
    """
    # The following signals are connected in/ called from control.py
    displayTextSignal = qtc.pyqtSignal(str)
    setLEDSignal = qtc.pyqtSignal(int, bool)
    # pinInEvent = qtc.pyqtSignal(int, bool)
    blinkerStart = qtc.pyqtSignal(int)
    blinkerStop = qtc.pyqtSignal()
    displayCaptionSignal = qtc.pyqtSignal(str, str)
    stopCaptionSignal = qtc.pyqtSignal()
    stopSimSignal = qtc.pyqtSignal()
    # Doesn't seem to be used
    checkPinsInEvent = qtc.pyqtSignal() 
    
    # The following signals are local
    # Need to avoid thread conflicts
    setTimeToNextSignal = qtc.pyqtSignal(int)
    # setTimeToEndSignal = qtc.pyqtSignal(int)
    setTimeToEndSignal = qtc.pyqtSignal()
    checkDualUnplugSignal = qtc.pyqtSignal(int)
    playRequestCorrectSignal = qtc.pyqtSignal()
    
    # NEW: Add a signal for thread-safe operator-only hello ending
    endOperatorOnlySignal = qtc.pyqtSignal()

    buzzInstace = vlc.Instance()
    buzzPlayer = buzzInstace.media_player_new()
    buzzPlayer.set_media(buzzInstace.media_new_path("/home/piswitch/Apps/sb-audio/buzzer.mp3"))
    buzzEvents = buzzPlayer.event_manager()

    toneInstace = vlc.Instance()
    tonePlayer = toneInstace.media_player_new()
    toneEvents = tonePlayer.event_manager()
    toneMedia = toneInstace.media_new_path("/home/piswitch/Apps/sb-audio/outgoing-ring.mp3")
    tonePlayer.set_media(toneMedia)

    vlcInstance = vlc.Instance()
    vlcPlayer = vlcInstance.media_player_new()
    vlcEvent = vlcPlayer.event_manager()

    dualUnplugTimer = qtc.QTimer()
    dualUnplugTimer.setSingleShot(True)
    # connect defined in _init_    

    def __init__(self):
        super().__init__()
        self.callInitTimer = qtc.QTimer()
        self.callInitTimer.setSingleShot(True)
        self.callInitTimer.timeout.connect(self.initiateCall)
        # signal is calling function setTimeToNext which calls callInitTimer
        # self.setTimeToNextSignal.connect(self.setTimeToNext)
        self.setTimeToNextSignal.connect(self.callInitTimer.start)

        self.reconnectTimer = qtc.QTimer()
        self.reconnectTimer.setSingleShot(True)
        self.reconnectTimer.timeout.connect(self.reCall)

        self.resetEndTimer = qtc.QTimer()
        self.resetEndTimer.setSingleShot(True)
        # self.resetEndTimer.timeout.connect(self.stopSimSignal.emit())
        self.resetEndTimer.timeout.connect(self.resetAtEnd)

        self.playRequestCorrectSignal.connect(self.playRequestCorrect)
        self.setTimeToEndSignal.connect(self.startEndTimer)

        # signal calls timeer directly
        self.checkDualUnplugSignal.connect(self.dualUnplugTimer.start)
        self.dualUnplugTimer.timeout.connect(self.checkDualUnplug)
        
        # NEW: Connect the thread-safe signal to the actual handler
        self.endOperatorOnlySignal.connect(self.handleEndOperatorOnly)

        self.reset()

    def reset(self):
        self.stopAllAudio()
        self.stopTimers()

        # Put pinsIn here in model where it's used more often
        # rather than in control which would require a lot of signaling.
        self.pinsIn = [False,False,False,False,False,False,False,False,False,False,False,False,False,False]
        
        self.currConvo = 0
        self.currCallerIndex = 0
        self.currCalleeIndex = 0
        # self.whichLineInUse = -1
        self.currStopTime = 0
        self.currPersonIdx = 0

        self.incrementJustCalled = False
        # self.reCallLine = 0 # Workaround timer not having params
        self.silencedCallLine = 0 # Workaround timer not having params
        # self.requestCorrectLine = 0 # Workaround timer not having params

        self.NO_UNPLUG_STATUS = 0
        self.WRONG_NUM_IN_PROGRESS = 1
        self.OP_ONLY_IN_PROGRESS = 2
        self.REPLUG_IN_PROGRESS = 3
        self.CALLER_UNPLUGGED = 5

        self.phoneLine = {
                "isEngaged": False,
                "unPlugStatus": self.NO_UNPLUG_STATUS,
                "caller": {"index": 99, "isPlugged": False},
                "callee": {"index": 99, "isPlugged": False}
                # "audioTrack": vlc.MediaPlayer("/home/piswitch/Apps/sb-audio/1-Charlie_Operator.mp3")
            }

        # self.displayTextSignal.emit("Keep your ears open for incoming calls!")

    def stopTimers(self):
        if self.callInitTimer.isActive():
            self.callInitTimer.stop()
        if self.reconnectTimer.isActive():
            self.reconnectTimer.stop()
        # if self.silencedCalTimer.isActive():
        #     self.silencedCalTimer.stop()


    def stopAllAudio(self):
        # if self.callInitTimer.isActive():
        #     self.callInitTimer.stop()

        self.buzzPlayer.stop()
        self.tonePlayer.stop()
        # self.vlcPlayers[0].stop()
        self.vlcPlayer.stop()

    def setPinIn(self, pinIdx, value):
        self.pinsIn[pinIdx] = value

    # Remove
    # def getPinInLine(self, pinIdx):
    #     return self.pinsInLine[pinIdx]
    
    # Used by wiggle detect in control
    def getIsPinIn(self, pinIdx):
        return self.pinsIn[pinIdx]

    def initiateCall(self):
        self.incrementJustCalled = False

        if (self.currConvo < 9):
            print(f'Setting currCallerIndex to {conversations[self.currConvo]["caller"]["index"]}'
                  f' currConvo: {self.currConvo}')
            self.currCallerIndex =  conversations[self.currConvo]["caller"]["index"]
            # Set "target", person being called
            self.currCalleeIndex = conversations[self.currConvo]["callee"]["index"]
            # This just rings the buzzer. Next action will
            # be when user plugs in a plug 
            # buzzTrack.volume = .6   

            self.buzzEvents.event_attach(vlc.EventType.MediaPlayerEndReached, 
                self.restartOnTimeout) 

            self.buzzPlayer.play()
            self.blinkerStart.emit(conversations[self.currConvo]["caller"]["index"])
            self.displayTextSignal.emit("Incoming call..")
            
            print(f'- New convo {self.currConvo} being initiated by: ' 
                    f'{persons[conversations[self.currConvo]["caller"]["index"]]["name"]}')
        else:
            # Play congratulations
            print("Congratulations - done!")
            self.playFinished()

    def playHello(self, _currConvo): # , lineIndex
        # print(" -- got to playHello")
        media = self.vlcInstance.media_new_path("/home/piswitch/Apps/sb-audio/" + 
            conversations[_currConvo]["helloFile"] + ".mp3")
        self.vlcPlayer.set_media(media)
        # For convo idxs 3 and 7 there is no full convo, so end after hello.
        # Attach event before playing
        if (_currConvo == 3 or  _currConvo == 8):
            print(f" -- got to currConv = 3 or 8 -- Operator only ")
            # Set call status to operator only
            self.phoneLine["unPlugStatus"] = self.OP_ONLY_IN_PROGRESS
            self.vlcEvent.event_attach(vlc.EventType.MediaPlayerEndReached, 
                self.endOperatorOnlyHello) #  _currConvo, 

        # Proceed with playing -- event may or may not be attached            
        self.vlcPlayer.play()
        # Send msg to screen
        self.displayCaptionSignal.emit('hello', conversations[_currConvo]["helloFile"])

    def endOperatorOnlyHello(self, event): # , lineIndex
        """
        This is called from VLC thread - we need to use signals to get back to main thread
        """
        print("  - VLC callback endOperatorOnlyHello - emitting signal to main thread")
        print(f'  - event: {event}')
        
        # Don't do any timer operations here - just emit a signal to the main thread
        if event is not None:
            try:
                self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)
            except:
                pass
        
        # Emit signal to main thread to handle the actual logic
        self.endOperatorOnlySignal.emit()

    def handleEndOperatorOnly(self):
        """
        This runs in the main thread and can safely start timers
        """
        print("  - handleEndOperatorOnly in main thread")
        
        #  supress further callbacks self.supressCallback
        # Don't know what this did in software proto
        # setHelloOnlyCompleted(lineIndex)
        self.clearTheLine() # lineIndex

        # Check if we've already incremented - this is the key change
        if not self.incrementJustCalled:
            print(f" - Hello-only ended.  Bump currConvo from {self.currConvo}")
            self.incrementJustCalled = True
            self.currConvo += 1
            # Now this will work because we're in the main thread
            self.setTimeToNextSignal.emit(1000)
        else:
            print(f" - Hello-only ended, but currConvo already incremented to {self.currConvo}")

    def playConvo(self, currConvo): # , lineIndex
        """
        This just plays the outgoing tone and then starts the full convo
        """
        print(f" -- got to play convo, currConvo: {currConvo}")
        # Long VLC way of creating callback
        self.toneEvents.event_attach(vlc.EventType.MediaPlayerEndReached, 
            self.playFullConvo, currConvo) # playFullConvo(currConvo, lineIndex)
        self.tonePlayer.set_media(self.toneMedia)
        self.tonePlayer.play()

    def playFullConvo(self, event, _currConvo):
        # Stop tone events from calling more times
        # print("  - About to detach toneEvent playFullConvo")
        if ( event != None):
            self.toneEvents.event_detach(vlc.EventType.MediaPlayerEndReached)

        print(f" -- PlayFullConvo {_currConvo}")
        # Set callback for convo track finish
        self.vlcEvent.event_attach(vlc.EventType.MediaPlayerEndReached, 
            self.setCallCompleted) #  _currConvo, 
        media = self.vlcInstance.media_new_path("/home/piswitch/Apps/sb-audio/" + 
            conversations[_currConvo]["convoFile"] + ".mp3")
        self.vlcPlayer.set_media(media)
        self.vlcPlayer.play()
        self.displayCaptionSignal.emit('convo', conversations[_currConvo]["convoFile"])

    def playWrongNum(self, pluggedPersonIdx): # , lineIndex
        print(f" -- [2] got to play wrong number, currConvo: {self.currConvo}")
        # Long VLC way of creating callback
        self.toneEvents.event_attach(vlc.EventType.MediaPlayerEndReached, 
            self.playFullWrongNum, pluggedPersonIdx) # playFullConvo(currConvo, lineIndex)
        self.tonePlayer.set_media(self.toneMedia)
        self.tonePlayer.play()

    def playFullWrongNum(self, event, pluggedPersonIdx): # , lineIndex
        # wrongNumFile = persons[pluggedPersonIdx]["wrongNumFile"]
        # disable event
        print("  - About to detacth toneEventin playFullWrongNum")

        self.toneEvents.event_detach(vlc.EventType.MediaPlayerEndReached) 

        self.displayTextSignal.emit(persons[pluggedPersonIdx]["wrongNumText"])

        print(f"  -- Play Wrong Num person {pluggedPersonIdx}")
        # Set callback for wrongNUm track finish

        self.vlcEvent.event_attach(vlc.EventType.MediaPlayerEndReached, 
            self.startPlayRequestCorrect) #  _currConvo, 
        
        media = self.vlcInstance.media_new_path("/home/piswitch/Apps/sb-audio/" + 
            persons[pluggedPersonIdx]["wrongNumFile"] + ".mp3")
        self.vlcPlayer.set_media(media)
        self.vlcPlayer.play()


    def startPlayRequestCorrect(self, event): # , lineIndex
        print("  - About to detach vlcEvent in startPlayRequestCorrect")

        if (event is not None ):
            self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)

        # self.requestCorrectLine = lineIndex
        self.playRequestCorrectSignal.emit()
        # self.requestCorrectTimer.start(1000)

    # def startRequestCorrectTimer(self):
    #     self.requestCorrectTimer.start(500)

    # Reply from caller saying who caller really wants
    def playRequestCorrect(self):
        print(f"  - got to playRequestCorrect, currConvo: {self.currConvo}")
        # Transcript for correction
        self.displayTextSignal.emit(conversations[self.currConvo]["retryAfterWrongText"])

        print("  - About to detach vlcEvent in PlayRequestCorrect")
        self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached) 


        media = self.vlcInstance.media_new_path("/home/piswitch/Apps/sb-audio/" + 
            conversations[self.currConvo]["retryAfterWrongFile"] + ".mp3")
        
        self.vlcPlayer.set_media(media)
        self.vlcPlayer.play()
        # At this point we hope user unplugs wrong number
        # Will be handled by "unPlug"

    def playFinished(self):
        self.toneEvents.event_detach(vlc.EventType.MediaPlayerEndReached)         

        self.displayTextSignal.emit("Congratulations -- you finished your first shift as a switchboard operator!")
        # print(f"-- PlayFullConvo {_currConvo}, lineIndex: {lineIndex}")

        media = self.vlcInstance.media_new_path("/home/piswitch/Apps/sb-audio/" + 
            "FinishedActivity.mp3")
        self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)

        self.vlcPlayer.set_media(media)

        self.vlcEvent.event_attach(vlc.EventType.MediaPlayerEndReached, 
            self.restartOnEndTimeout) 

        self.vlcPlayer.play()

    # def setTimeToNext(self, timeToWait):
    #     self.callInitTimer.start(timeToWait)   
             

    def setTimeReCall(self, _currConvo): 
        print("got to setTimeReCall")
        # currConvo is already global
        self.reconnectTimer.start(1000)
        # recconectTimer will call reCall

    def reCall(self):
        print("got to reCall")
        # Hack: receives reCallLine globally 
        self.playHello(self.currConvo) #, self.reCallLine
        # calling playHello direclty with callback would send event param

    def handlePlugIn(self, personIdx):
        """triggered by control.py
        """
        print(f' - Start handlePlugIn, personIdx: {personIdx}'
              f' is caller plugged: {self.phoneLine["caller"]["isPlugged"]}')
        # ********
        # Fresh plug-in -- aka caller wasn't plugged yet
        # Is this new use of this line -- caller has not been plugged in.
        # *******/
        if (not self.phoneLine["caller"]["isPlugged"]): # New line - Caller not plugged
            # Did user plug into the actual caller?
            if personIdx == self.currCallerIndex: # Correct caller
                # Turn this LED on
                self.setLEDSignal.emit(personIdx, True)
                # Set this person's jack to plugged
                self.setPinIn(personIdx, True)

                # Set this line as having caller plugged
                self.phoneLine["caller"]["isPlugged"] = True
                # Set identity of caller on this line
                self.phoneLine["caller"]["index"] = personIdx;				
                # print(f' - Just set caller {self.phoneLine["caller"]["index"]} to True')

                # Set this line in use only we have gotten this success
                # self.whichLineInUse = lineIdx

                # See software app for extended debug message here
                # Stop Buzzer. 
                self.buzzPlayer.stop()
                # Blinker handdled in control.py
                self.blinkerStop.emit()

                # print(f" ++ New plugin- prev line in use: {self.prevLineInUse}")

                #  Handle case where caller was unplugged
                if (self.phoneLine["unPlugStatus"] == self.CALLER_UNPLUGGED):
                    print(f"  - Caller was unplugged")
                    """ more logic here  
                    """
                    if (self.phoneLine["callee"]["isPlugged"] == True):
                        # if (correct callee??)
                        # Stop Hello/Request
                        self.vlcPlayer.stop()
                        # set line engaged
                        self.phoneLine["unPlugStatus"] = self.NO_UNPLUG_STATUS
                        self.phoneLine["isEngaged"] = True
                        self.phoneLine["caller"]["isPlugged"] = True
                        # Start conversation without the ring
                        # For now anyway can't play full convo without sending event so


                        # self.playFullConvoNoEvent(self.currConvo)
                        print("  - playFullConvo w/o event ")
                        # None param is for non-existant event
                        self.playFullConvo(None, self.currConvo)


                    else:
                        print('   We should not get here');

                else: # Regular, just play incoming Hello/Request
                    self.playHello(self.currConvo) 
                
                # Set prev for use in next call. Here??
                # print(f"setting prev line in use from {p}")
                # self.prevLineInUse = self.whichLineInUse
            else:
                print("wrong jack -- or wrong line")
                self.displayTextSignal.emit("That's not the jack for the person who is asking you to connect!")

        #********
        # Other end of the line -- caller is plugged, so this must be the callee
        #********/
        else: # caller is plugged
			# Ignore the following if this is an operator-only call in progress
            print(' -- else caller plugged. unPlugStatus: ' + str(self.phoneLine["unPlugStatus"]))
            if (not self.phoneLine["unPlugStatus"] == self.OP_ONLY_IN_PROGRESS):

                # Whether or not this is correct callee -- turn LED on.
                self.setLEDSignal.emit(personIdx, True)
                # Set pinsIn True
                self.setPinIn(personIdx, True)
                # Stop the hello operator track,  whether this is the correct
                # callee or not
                self.vlcPlayer.stop()
                # Also stop captions
                self.stopCaptionSignal.emit()


                # Set callee -- used by unPlug even if it's the wrong number
                self.phoneLine["callee"]["index"] = personIdx
                if (personIdx == self.currCalleeIndex): # Correct callee
                    print(f" - Plugged into correct callee, idx: {personIdx}")
                    # Set this line as engaged
                    self.phoneLine["isEngaged"] = True
                    # Also set line callee plugged
                    self.phoneLine["callee"]["isPlugged"] = True

                    # # Silence incoming Hello/Request, if necessary
                    # self.vlcPlayers[lineIdx].stop()
                    self.playConvo(self.currConvo)

                else: # Wrong number
                    print(" -- |1| just plugged into wrong number")
                    self.phoneLine["unPlugStatus"] = self.WRONG_NUM_IN_PROGRESS

                    self.playWrongNum(personIdx) 

            else:
        
                print("got to Tressa erroneous plug-in")


    def handleUnPlug(self, personIdx): 
        """ triggered by control.py
        """
        print( f" - Index {personIdx} Unplugged with line status of: {self.phoneLine['unPlugStatus']}\n"
               f"     while line isEngaged = {self.phoneLine['isEngaged']}"
            )
        # if not during restart!

        # ---- Conversation in progress --- 
        if (self.phoneLine["isEngaged"]):
            # If conversation is in progress -- engaged (implies correct callee)
            print(f'  - Unplugging a call in progress person id: {persons[personIdx]["name"]} ' )
            # Get stop time
            stopTime = self.vlcPlayer.get_time()
            # print(f'  -- stop time: {stopTime}')

            # Stop the audio
            self.vlcPlayer.stop()
            # Stop subtitles
            self.stopCaptionSignal.emit()
            # Clear Transcript 
            self.displayTextSignal.emit("Call disconnected..")

            # Check to see if Both were unplugged
            # Maybe look at pinsIn -- if only one was unplugged then the other 
            # pin should be in. Be aware of the 150 finishCheck timeer -- 
            # Do my business here within that time 
            # And don't forget to check enough time to decide whether to start 
            # over or continue.
            # if

            self.currPersonIdx = personIdx
            self.currStopTime = stopTime
            print(' - got to engaged unplug, calling check dual')
            # Can't call timer directly, so setting temp variables
            # and starting timer with this signal
            self.checkDualUnplugSignal.emit(90)
            
        # ---- Conversation NOT in progress --- 
        else:   
            # Phone line is not engaged -- isEngaged == False
            print(f' - unplug while not engaged, callee index: {self.phoneLine["callee"]["index"]}'
                  f'    caller index: {self.phoneLine["caller"]["index"]}')

            print(f'  -- unplugStatus: {self.phoneLine["unPlugStatus"]}')
            # if unplugStatus is 1 that is WrongNumInProg and we should __?
            # I get here whether I've unplugged during the wrong answer or after it's finished
            # If wrong number, hmm need plug status for wrong number
            # if (self.phoneLine["unPlugStatus"] == 1): # WRONG_NUM_IN_PROGRESS
            #     print(' -- [3] got to unplug while WrongINProg')
            #     self.vlcPlayer.stop() 
            #     self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)
            #     # if (personIdx < 99):
            #     self.setLEDSignal.emit(personIdx, False)
            #     # clear the unplug status
            #     self.phoneLine["unPlugStatus"] = self.NO_UNPLUG_STATUS

            #     print(' -- [3.2] would be doing: startPlayRequestCorrect')
            #     # self.startPlayRequestCorrect(None)

            # # First, maybe this is an unplug of "old" call to free up the plugg
            # # caller would be plugged
            # elif (self.phoneLine["caller"]["isPlugged"] == True):
            if (self.phoneLine["caller"]["isPlugged"] == True):
                # Caller has initiated a call

                # If this is the caller being unplugged (erroneously or early)
                # Correct caller unplugging?
                if (personIdx == self.phoneLine["caller"]["index"]):
                    print("     caller unplugged")
                    stopTime = self.vlcPlayer.get_time()
                    self.vlcPlayer.stop() 
                    #  LED handled by either condition below
                    # If this is a hello only call # And if we're close enough to the end
                    if ((self.currConvo == 3 or  self.currConvo == 8) and
                        stopTime > conversations[self.currConvo]["okTimeHello"]):
                        # Close enough to end, move on 
                        print(f'  - stopped operator only caller with time: {stopTime}')
                        # Use the thread-safe signal instead of calling directly
                        self.endOperatorOnlySignal.emit()
                    else:
                        self.clearTheLine()
                        self.callInitTimer.start(1000)
                elif (self.phoneLine["unPlugStatus"] == self.WRONG_NUM_IN_PROGRESS):
                    # Unplugging wrong num
                    print(f' -- |2| Unplug on wrong number, personIdx: {personIdx}')
                    self.vlcPlayer.stop() 
                    # Cover for before personidx defined
                    if (personIdx < 99):
                        self.setLEDSignal.emit(personIdx, False)
                    # clear the unplug status
                    self.phoneLine["unPlugStatus"] = self.NO_UNPLUG_STATUS
                else: # Not unplugging wrong - do nothing
                    print(" just unplugging to free up a plug")

            else: # caller not plugged
                print(" * nothing going on, just unplugging ")

        # After all is said and done, this was unplugged, So, set pinIn False
        # self.setPinInLine(personIdx, -1)
        self.setPinIn(personIdx, False)
        print(f" - pin {personIdx} is now {self.pinsIn[personIdx]}")

    def setDualUnplugTimer(self):
        # Timer will call 
        self.dualUnplugTimer.start(90)

    def checkDualUnplug(self):
        print(' - got to checkDualUnplug, need to actually check!')
        # if such & so do somethingelse
        self.continueSingleEngagedUnplug(self.currPersonIdx, self.currStopTime)

    def continueSingleEngagedUnplug(self, personIdx, stopTime):
        # print(' - got to continue single unplug')
        # callee just unplugged
        if (self.phoneLine["callee"]["index"] == personIdx):  
            print('   Unplugging callee. stopTime: ' + str(stopTime))
            # Turn off callee LED
            self.setLEDSignal.emit(self.phoneLine["callee"]["index"], False)

            # If Early in call, retry
            if (stopTime < conversations[self.currConvo]["okTimeConvo"]):
                # Restart this answer to cal
                # Mark callee unplugged
                self.phoneLine["callee"]["isPlugged"] = False
                self.phoneLine["isEngaged"] = False
                # stop captions
                self.stopCaptionSignal.emit()
                # Leave caller plugged in, replay hello
                self.setTimeReCall(self.currConvo)
            else:
                # Late in call -- end convo and move on
                print(f'  - stopped with time: {stopTime}')
                self.setCallCompleted(self)

        # caller unplugged
        elif (self.phoneLine["caller"]["index"] == personIdx): 
            print(" Caller just unplugged")
            self.phoneLine["caller"]["isPlugged"] = False
            self.phoneLine["isEngaged"] = False
            # Also
            self.phoneLine["unPlugStatus"] = self.CALLER_UNPLUGGED
            # Turn off caller LED
            self.setLEDSignal.emit(self.phoneLine["caller"]["index"], False)
            # signal calls callInitTimer which calls initiateCall	
            self.setTimeToNextSignal.emit(1000)					
        else: 
            print('    This should not happen')

    def setCallCompleted(self, event=None): #, _currConvo, lineIndex
        # Disable callback if present
        if (event != None):
            self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)
                
        print(f" -- setCallCompleted. Convo: {self.currConvo}")
        # Stop call
        self.stopCall()

        # # print("other line has neither caller nor callee plugged")
        # if (self.phoneLines[otherLineIdx]["unPlugStatus"] == self.REPLUG_IN_PROGRESS):

        # Workaround to stop double calling
        if not self.incrementJustCalled:
            self.incrementJustCalled = True
            print(f' -  increment from {self.currConvo} and start regular timer for next call.')
            # Uptick currConvo here, when call is comlete
            self.currConvo += 1
            # Use signal rather than calling callInitTimer bcz threads
            self.setTimeToNextSignal.emit(1000)

        # When call 0 ends, do nothing. But how do I kmow this is is 0 ending since
        # currConvo has already been incremented to 1. When 1 ends I do want to increment.    
        
        # if (self.interruptingCallInHasBeenInitiated):
        #     print("-- Interrupting call has been initiated -- and is ending, do nothing.")    

    def stopCall(self): # , lineIndex
        self.clearTheLine()
        # Reset volume -- in this line was silenced by interrupting call
        # self.vlcPlayers[self.prevLineInUse].audio_set_volume(100)

    def clearTheLine(self):
        # Clear the line settings
        self.phoneLine["caller"]["isPlugged"] = False
        self.phoneLine["callee"]["isPlugged"] = False
        self.phoneLine["isEngaged"] = False
        self.phoneLine["unPlugStatus"] = self.NO_UNPLUG_STATUS
        # self.prevLineInUse = -1
        # Turn off the LEDs
        self.setLEDSignal.emit(self.phoneLine["caller"]["index"], False)
        # Can't turn off callee led if callee index hasn't been defined
        # print(f'About to try to turn off .callee.index: {self.phoneLines[lineIdx]["callee"]["index"]}')
        if (self.phoneLine["callee"]["index"] < 90):
            # console.log('got into callee index not null');
            self.setLEDSignal.emit(self.phoneLine["callee"]["index"], False)

    def handleStart(self):
        """Just for startup
        """
        print(" - got to model.handleStart")
        # Set callback for welcome track finish
        self.vlcEvent.event_attach(vlc.EventType.MediaPlayerEndReached, 
            self.afterWelcome)  
        media = self.vlcInstance.media_new_path("/home/piswitch/Apps/sb-audio/Welcome.mp3")
        self.vlcPlayer.set_media(media)
        self.vlcPlayer.play()
        # self.displayCaptionSignal.emit('convo', conversations[_currConvo]["convoFile"])
        self.displayTextSignal.emit("Welcome to the switchboard game. \nIt's your turn to be a switchboard operator! \nHere comes the first call.")

    def afterWelcome(self, event):
        self.setTimeToNextSignal.emit(1000) # calls setTimeToNext

    def restartOnTimeout(self, event):
        print(' - auto starting reset')
        self.buzzEvents.event_detach(vlc.EventType.MediaPlayerEndReached)
        self.blinkerStop.emit()
        self.stopSimSignal.emit()

    def restartOnEndTimeout(self, event):
        print(' - Starting reset after End')
        self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)

        # Can't call endTimer directly, so signal
        # This signal will call startEndTimer
        self.setTimeToEndSignal.emit()
        # Couldn't use setTimeToNextSignal because that's hard-wired to starting calls

    def startEndTimer(self):
        # Timer will call self.stopSimSignal.emit()
        self.resetEndTimer.start(2000)

    def resetAtEnd(self):
        # Maybe this could go directly in callback?
        self.stopSimSignal.emit()

    def detachAllEventHandlers(self):
        # Detach all VLC event handlers
        try:
            self.buzzEvents.event_detach(vlc.EventType.MediaPlayerEndReached)
        except:
            pass
        
        try:
            self.toneEvents.event_detach(vlc.EventType.MediaPlayerEndReached)
        except:
            pass
        
        try:
            self.vlcEvent.event_detach(vlc.EventType.MediaPlayerEndReached)
        except:
            pass

            