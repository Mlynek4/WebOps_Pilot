import { useState, useRef } from 'react'
import { FluentProvider, teamsDarkTheme, Subtitle1, Input, Button, Text } from '@fluentui/react-components'
import useStyles from './appStyles'
import './App.css'

function App() {
  const styles = useStyles()
  const [chatHistory, setChatHistory] = useState<{ role: 'user' | 'assistant'; content: string }[]>([])
  const [inputValue, setInputValue] = useState('')
  const chatBodyRef = useRef<HTMLDivElement>(null)

  const handleSendMessage = () => {
    setChatHistory((prev) => [...prev, { role: 'user', content: inputValue }])
    setInputValue('')

    setTimeout(() => {
      if (chatBodyRef.current) {
        chatBodyRef.current.scrollTop = chatBodyRef.current.scrollHeight
      }
    }, 0)
  }

  return (
    <FluentProvider theme={teamsDarkTheme} style={{padding: '0', margin: '0'}}>
      <div className={styles.root}>
        <div className={styles.card}>
          <Subtitle1 align='center' style={{ paddingBottom: '16px' }}>
            VisSegBud reactivation
          </Subtitle1>
          <div ref={chatBodyRef} style={{ maxHeight: '50vh', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <Text className={styles.hintText}>What would you like to do today?</Text>
            {
              chatHistory.map((msg, idx) => (
                <div key={`${msg}-${idx}`} style={{ textAlign: msg.role === 'user' ? 'right' : 'left', marginBottom: '4px' }}>
                  <Text style={{ display: 'inline-block', padding: '8px', borderRadius: '4px', background: msg.role === 'user' ? 'rgba(0, 120, 212, 0.8)' : 'rgba(255, 255, 255, 0.1)', color: '#fff' }}>
                    {msg.content}
                  </Text>
                </div>
              ))
            }
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
