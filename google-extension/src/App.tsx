import { useState, useRef, useEffect } from 'react'
import axios from 'axios'
import Typewriter from 'typewriter-effect'
import { FluentProvider, teamsDarkTheme, Subtitle1, Input, Button, Text, mergeClasses } from '@fluentui/react-components'
import useStyles from './appStyles'
import './App.css'

function App() {
  const styles = useStyles()
  const [chatHistory, setChatHistory] = useState<{ role: 'user' | 'assistant'; content: string }[]>([])
  const [inputValue, setInputValue] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const chatBodyRef = useRef<HTMLDivElement>(null)
  const lastMessageRef = useRef<HTMLDivElement>(null)

  async function getContext() {
    const res = await chrome.runtime.sendMessage({ type: "GET_CONTEXT" });
    if (!res?.ok) throw new Error(res?.error || "GET_CONTEXT failed");
    return res.pageContext;
  }

  const sessionId = 'testid';

  const fetchBackendResponse = async (message: string) => {
    try {
      const pageContext = await getContext();
      const response = await axios.post('http://127.0.0.1:8000/turn', {
        session_id: sessionId,
        user_text: message,
        page_context: pageContext
      }, {
        headers: { 'Content-Type': 'application/json' }
      })
      return response.data.assistant_text
    } catch (error) {
      console.error('Error calling backend:', error)
      return 'Failed to get response from backend'
    }
  }

  const handleSendMessage = async () => {
    if (!inputValue.trim()) return

    setChatHistory((prev) => [...prev, { role: 'user', content: inputValue }])
    setInputValue('')
    setIsLoading(true)

    try {
      const botResponse = await fetchBackendResponse(inputValue)
      setChatHistory((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: botResponse,
        },
      ])
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    if (lastMessageRef.current) {
        lastMessageRef.current.scrollIntoView({ behavior: 'smooth' })
      }
  }, [chatHistory, isLoading]);

  return (
    <FluentProvider theme={teamsDarkTheme} style={{padding: '0', margin: '0'}}>
      <div className={styles.root}>
        <div className={styles.card}>
          <Subtitle1 align='center' style={{ paddingBottom: '16px' }}>
            Web Application Def*cker
          </Subtitle1>
          <div ref={chatBodyRef} className={styles.chatBody}>
            <Text className={styles.hintText}>What would you like to do today?</Text>
            {
              chatHistory.map((msg, idx) => (
                <div key={`${msg}-${idx}`} className={msg.role === 'user' ? styles.messageItemUser : styles.messageItemAssistant}>
                  <Text
                    className={styles.messageBubble}
                    style={{ background: msg.role === 'user' ? 'rgba(0, 120, 212, 0.8)' : 'rgba(255, 255, 255, 0.1)' }}
                  >
                    {msg.content}
                  </Text>
                </div>
              ))
            }
            {isLoading && (
              <div className={styles.loadingRow}>
                <div className={styles.loadingIndicatorContainer}>
                  <div className={styles.bar}></div>
                  <div className={mergeClasses(styles.bar, styles.bar2)}></div>
                  <div className={mergeClasses(styles.bar, styles.bar3)}></div>
                </div>
                <Text color='white' className={styles.thinkingText}>
                  <Typewriter
                    options={{
                      strings: ['Thinking...', 'Exploring...', 'Wondering...', 'Analyzing...'],
                      autoStart: true,
                      loop: true,
                      delay: 10,
                      cursor: '',
                      deleteSpeed: 10,
                    }}
                  />
                </Text>
              </div>
            )}
            <div ref={lastMessageRef}/>
          </div>
          <div className={styles.inputRow}>
            <Input 
              className={styles.input} 
              placeholder="Tell me what you would like..." 
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSendMessage()}
            />
            <Button onClick={handleSendMessage}>Send</Button>
          </div>
        </div>
      </div>
    </FluentProvider>
  )
}

export default App

